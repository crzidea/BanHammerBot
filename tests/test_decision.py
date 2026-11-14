import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from banhammerbot.api import JSONAPI, APIError, ImageAsset, Telegram
from banhammerbot.config import Config
from banhammerbot.decision import Decision, InvalidDecision, Jev


def response(ad=0.99, spam=0.05, profile=0.7):
    return {
        "model": "jev-1.14.0",
        "answers": {
            "unsolicited_ad": {"type": "noul", "noul": ad},
            "spam": {"type": "noul", "noul": spam},
            "profile_promotion": {"type": "noul", "noul": profile},
        },
    }


class DecisionTests(unittest.TestCase):
    def test_profile_alone_does_not_trigger_ban(self):
        self.assertEqual(Decision.parse(response(0.1, 0.2, 1)).violation, 0.2)

    def test_invalid_probabilities_are_rejected(self):
        for value in (True, False, None, "0.99", float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(InvalidDecision):
                Decision.parse(response(ad=value))

    def test_invalid_shapes_are_rejected(self):
        missing = response()
        del missing["answers"]["spam"]
        wrong_type = response()
        wrong_type["answers"]["spam"]["type"] = "choice"
        for body in ([], None, {}, {"answers": []}, missing, wrong_type):
            with self.subTest(body=body), self.assertRaises(InvalidDecision):
                Decision.parse(body)


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_multimodal_contract(self):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=response())

        config = Config("123:secret", "jev-secret", frozenset({-1001}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            decision = await Jev(client, config).decide(
                {"sender": {"first_name": "Deals"}, "current_message": {"text": "Buy now"}},
                {
                    "sender_avatar": ImageAsset("jpg", b"image"),
                    "current_message_image": ImageAsset("png", b"other image"),
                },
            )
        request = captured[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.headers["Authorization"], "Bearer jev-secret")
        self.assertEqual(request.headers["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(len(body["assets"]), 2)
        for label in ("sender_avatar", "current_message_image"):
            reference = body["state"]["images"][label]
            self.assertEqual(body["assets"][reference]["type"], "image")
            self.assertIn("data_base64", body["assets"][reference])
        self.assertNotIn("123:secret", request.content.decode())
        self.assertEqual(decision.violation, 0.99)

    async def test_no_images_omits_assets_and_experimental_header(self):
        def handler(request):
            self.assertNotIn("assets", json.loads(request.content))
            self.assertNotIn("TypeSafe-Danger-Zone-Images", request.headers)
            return httpx.Response(200, json=response())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Jev(client, Config("token", "key", frozenset({-1}))).decide({}, {})

    async def test_429_retries_then_succeeds(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            if count < 2:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json=response())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("banhammerbot.api.asyncio.sleep", new_callable=AsyncMock) as sleep:
                await JSONAPI(client, "jev").post("https://example.test", {})
                sleep.assert_awaited_once_with(2.0)
        self.assertEqual(count, 2)

    async def test_long_telegram_rate_limit_is_returned_without_early_retry(self):
        def handler(request):
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 60},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(APIError) as caught:
                await Telegram(client, "secret", 100).call("getMe")
        self.assertEqual(caught.exception.retry_after, 60)

    async def test_bad_credentials_not_retried_and_error_is_sanitized(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="secret-token and sensitive provider response")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(APIError) as caught:
                await Telegram(client, "secret-token", 100).call("getMe")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(calls), 1)

    async def test_timeouts_retry_boundedly_without_leaking_url(self):
        def handler(request):
            raise httpx.ReadTimeout("secret-token", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("banhammerbot.api.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaises(APIError) as caught:
                    await Telegram(client, "secret-token", 100).call("getMe")
        self.assertEqual(sleep.await_count, 2)
        self.assertNotIn("secret", str(caught.exception))

    async def test_non_json_response_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="<html>unavailable</html>"),
            )
        ) as client:
            with self.assertRaises(APIError):
                await JSONAPI(client, "jev").post("https://example.test", {})

    async def test_avatar_download_and_size_limit(self):
        for data, allowed in (
            (b"\xff\xd8\xff123", True),
            (b"\xff\xd8\xff" + b"x" * 200, False),
            (b"not a photo", False),
        ):
            with self.subTest(allowed=allowed, size=len(data)):

                def handler(request, data=data):
                    if request.url.path.endswith("getFile"):
                        return httpx.Response(
                            200,
                            json={
                                "ok": True,
                                "result": {"file_path": "photos/file.jpg"},
                            },
                        )
                    return httpx.Response(200, content=data)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    telegram = Telegram(client, "secret", 100)
                    if allowed:
                        self.assertEqual((await telegram.image("file-id")).data, data)
                    else:
                        with self.assertRaises(APIError):
                            await telegram.image("file-id")

    async def test_file_path_traversal_and_metadata_size_rejected(self):
        for info in ({"file_path": "../other"}, {"file_path": "photo.jpg", "file_size": 1000}):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, info=info: httpx.Response(
                        200, json={"ok": True, "result": info}
                    ),
                )
            ) as client:
                with self.assertRaises(APIError):
                    await Telegram(client, "secret", 100).image("file-id")
