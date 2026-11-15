import asyncio
import base64
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx


class APIError(Exception):
    """Error metadata with an optional sanitized Telegram description, never a raw body."""

    def __init__(
        self,
        service: str,
        code: int = 0,
        retry_after: float | None = None,
        *,
        method: str | None = None,
        chat_id: int | None = None,
        description: str | None = None,
        migrate_to_chat_id: int | None = None,
    ):
        self.service = service
        self.code = code
        self.retry_after = retry_after
        self.method = method
        self.description = description
        self.migrate_to_chat_id = migrate_to_chat_id
        operation = f"{service}.{method}" if method else service
        message = f"{operation} request failed (code {code})"
        if chat_id is not None:
            message += f" chat_id={chat_id}"
        if description:
            message += f": {description}"
        if migrate_to_chat_id is not None:
            message += f"; update TELEGRAM_CHAT_IDS to the migrated group ID {migrate_to_chat_id}"
        super().__init__(message)


def sanitize_description(value: Any, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
            value = value.replace(quote(secret, safe=""), "[redacted]")
    value = re.sub(r"https?://\S+", "[redacted URL]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d+:[A-Za-z0-9_-]+", "[redacted token]", value)
    value = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", value, flags=re.IGNORECASE)
    # Strip terminal control sequences and prevent an API description from forging log lines.
    value = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(value.split())[:300] or None


def retry_delay(value: Any) -> float | None:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


class JSONAPI:
    def __init__(self, client: httpx.AsyncClient, service: str, secrets: tuple[str, ...] = ()):
        self.client = client
        self.service = service
        self.secrets = secrets

    async def post(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict | None = None,
        request_timeout: float = 15,
        method: str | None = None,
        attempts: int = 3,
    ) -> Any:
        context = {"method": method}
        if self.service == "telegram" and type(payload.get("chat_id")) is int:
            context["chat_id"] = payload["chat_id"]
        for attempt in range(attempts):
            try:
                response = await self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                )
            except httpx.RequestError:
                error = APIError(self.service, **context)
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if response.is_success:
                    if body is None:
                        raise APIError(self.service, response.status_code, **context) from None
                    if self.service != "telegram":
                        return body
                    if isinstance(body, dict) and body.get("ok") is True and "result" in body:
                        return body["result"]
                    code = body.get("error_code", 0) if isinstance(body, dict) else 0
                else:
                    code = response.status_code
                delay = retry_delay(response.headers.get("Retry-After"))
                description = None
                migrated_id = None
                if self.service == "telegram":
                    if isinstance(body, dict):
                        description = sanitize_description(body.get("description"), self.secrets)
                        parameters = body.get("parameters")
                        if isinstance(parameters, dict):
                            delay = retry_delay(parameters.get("retry_after")) or delay
                            migrated_id = parameters.get("migrate_to_chat_id")
                            if type(migrated_id) is not int:
                                migrated_id = None
                code = code if type(code) is int else 0
                error = APIError(
                    self.service,
                    code,
                    delay,
                    description=description,
                    migrate_to_chat_id=migrated_id,
                    **context,
                )
                if code != 429 and not (isinstance(code, int) and 500 <= code < 600):
                    raise error
            # Long rate limits are surfaced to the runner instead of blocking moderation.
            if attempt == attempts - 1 or (
                error.retry_after is not None and error.retry_after > 10
            ):
                raise error from None
            await asyncio.sleep(error.retry_after if error.retry_after is not None else 2**attempt)
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ImageAsset:
    format: str
    data: bytes

    def as_json(self) -> dict:
        return {
            "type": "image",
            "format": self.format,
            "data_base64": base64.b64encode(self.data).decode("ascii"),
        }


class Telegram:
    def __init__(self, client: httpx.AsyncClient, token: str, max_image_bytes: int):
        self.client = client
        self.api = JSONAPI(client, "telegram", secrets=(token,))
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.max_image_bytes = max_image_bytes

    async def call(self, method: str, **params: Any) -> Any:
        if method == "restrictChatMember":
            try:
                async with asyncio.timeout(10):
                    return await self.api.post(
                        self.base_url + "/" + method,
                        params,
                        request_timeout=10,
                        method=method,
                        attempts=1,
                    )
            except TimeoutError:
                raise APIError("telegram", method=method) from None
        return await self.api.post(
            self.base_url + "/" + method,
            params,
            request_timeout=40,
            method=method,
        )

    async def image(self, file_id: str) -> ImageAsset:
        info = await self.call("getFile", file_id=file_id)
        path = info.get("file_path", "")
        if (
            not path
            or path.startswith("/")
            or ".." in path.split("/")
            or info.get("file_size", 0) > self.max_image_bytes
        ):
            raise APIError("telegram image")
        try:
            async with self.client.stream(
                "GET",
                self.file_url + "/" + quote(path, safe="/"),
                timeout=15,
            ) as response:
                if not response.is_success:
                    raise APIError("telegram image", response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.max_image_bytes:
                        raise APIError("telegram image too large")
        except httpx.RequestError:
            raise APIError("telegram image") from None
        if data.startswith(b"\xff\xd8\xff"):
            image_format = "jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            image_format = "png"
        else:
            raise APIError("unsupported image format")
        return ImageAsset(image_format, bytes(data))
