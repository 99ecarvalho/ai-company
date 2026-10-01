"""Tests for the /api/tts/synthesize proxy against a fake ai-tts upstream.

Uses only the stdlib unittest (pytest is not installed in the `web` container).

Run inside the web container:
  docker compose exec web python -m unittest app.tests.test_tts_proxy -v
"""
from __future__ import annotations

import json
import os
import unittest

from aiohttp import web
from fastapi import HTTPException

from app.main import _upstream_detail, tts_synthesize


class UpstreamDetail(unittest.TestCase):
    def test_string_detail(self):
        self.assertEqual(_upstream_detail('{"detail": "voice \'x\' is not installed"}'),
                         "voice 'x' is not installed")

    def test_validation_list_detail(self):
        body = json.dumps({"detail": [
            {"loc": ["body", "text"], "msg": "String should have at least 1 character", "type": "x"},
            {"loc": ["body", "voice"], "msg": "String should match pattern", "type": "y"},
        ]})
        self.assertEqual(_upstream_detail(body),
                         "String should have at least 1 character; String should match pattern")

    def test_non_json_body(self):
        self.assertEqual(_upstream_detail("Internal Server Error"), "Internal Server Error")


class TtsProxy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received: list[dict] = []
        self.reply = (200, b"RIFFfake")

        async def synthesize(request: web.Request) -> web.Response:
            self.received.append(await request.json())
            status, body = self.reply
            ctype = "audio/wav" if status == 200 else "application/json"
            return web.Response(status=status, body=body, content_type=ctype)

        app = web.Application()
        app.router.add_post("/synthesize", synthesize)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self._old_url = os.environ.get("TTS_URL")
        os.environ["TTS_URL"] = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        if self._old_url is None:
            os.environ.pop("TTS_URL", None)
        else:
            os.environ["TTS_URL"] = self._old_url

    async def _status(self, payload: dict) -> int:
        with self.assertRaises(HTTPException) as ctx:
            await tts_synthesize(payload, None)
        return ctx.exception.status_code

    async def test_text_only_returns_wav(self):
        resp = await tts_synthesize({"text": "  hello  "}, None)
        self.assertEqual(resp.media_type, "audio/wav")
        self.assertEqual(resp.body, b"RIFFfake")
        self.assertEqual(self.received, [{"text": "hello", "format": "wav"}])

    async def test_empty_voice_is_omitted(self):
        await tts_synthesize({"text": "hi", "voice": ""}, None)
        self.assertNotIn("voice", self.received[0])

    async def test_mp3_media_type(self):
        resp = await tts_synthesize({"text": "hi", "format": "mp3"}, None)
        self.assertEqual(resp.media_type, "audio/mpeg")

    async def test_local_validation_never_calls_upstream(self):
        for payload in ({"text": ""}, {"text": "   "}, {}, {"text": 123},
                        {"text": "x" * 5001}, {"text": "hi", "voice": "../x"},
                        {"text": "hi", "voice": "a.b"}, {"text": "hi", "format": "ogg"}):
            self.assertEqual(await self._status(payload), 422, payload)
        self.assertEqual(self.received, [])

    async def test_voice_not_installed_is_404(self):
        self.reply = (404, b'{"detail": "voice \'en_US-x\' is not installed"}')
        with self.assertRaises(HTTPException) as ctx:
            await tts_synthesize({"text": "hi", "voice": "en_US-x"}, None)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not installed", ctx.exception.detail)

    async def test_upstream_422_list_becomes_string(self):
        self.reply = (422, b'{"detail": [{"loc": ["body"], "msg": "bad input", "type": "x"}]}')
        with self.assertRaises(HTTPException) as ctx:
            await tts_synthesize({"text": "hi"}, None)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail, "bad input")

    async def test_upstream_500_is_502(self):
        self.reply = (500, b'{"detail": "synthesis failed"}')
        self.assertEqual(await self._status({"text": "hi"}), 502)


if __name__ == "__main__":
    unittest.main()
