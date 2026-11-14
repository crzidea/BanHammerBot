import asyncio
import base64
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx


class APIError(Exception):
    """Sanitized error: never include token-bearing URLs or provider response bodies."""

    def __init__(self, service: str, code: int = 0, retry_after: float | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"{service} request failed (code {code})")


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
    def __init__(self, client: httpx.AsyncClient, service: str):
        self.client = client
        self.service = service

    async def post(
        self, url: str, payload: dict, *, headers: dict | None = None, request_timeout: float = 15
    ) -> Any:
        for attempt in range(3):
            try:
                response = await self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                )
            except httpx.RequestError:
                error = APIError(self.service)
            else:
                if response.is_success:
                    try:
                        body = response.json()
                    except ValueError:
                        raise APIError(self.service, response.status_code) from None
                    if self.service != "telegram":
                        return body
                    if isinstance(body, dict) and body.get("ok") is True and "result" in body:
                        return body["result"]
                    code = body.get("error_code", 0) if isinstance(body, dict) else 0
                else:
                    code = response.status_code
                delay = retry_delay(response.headers.get("Retry-After"))
                if self.service == "telegram":
                    try:
                        delay = retry_delay(response.json()["parameters"]["retry_after"]) or delay
                    except (ValueError, TypeError, KeyError):
                        pass
                error = APIError(self.service, code, delay)
                if code != 429 and not (isinstance(code, int) and 500 <= code < 600):
                    raise error
            # Long rate limits are surfaced to the runner instead of blocking moderation.
            if attempt == 2 or (error.retry_after is not None and error.retry_after > 10):
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
        self.api = JSONAPI(client, "telegram")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.max_image_bytes = max_image_bytes

    async def call(self, method: str, **params: Any) -> Any:
        return await self.api.post(self.base_url + "/" + method, params, request_timeout=40)

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
