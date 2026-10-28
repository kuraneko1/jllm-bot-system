import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.imagegen.service import AnimaClient, ImageGenError


class AnimaClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = None

        async def generate(request):
            self.payload = await request.json()
            if getattr(self, "bad", False):
                return web.json_response({})
            return web.json_response({"job_id": "job-1"})

        async def job(request):
            return web.json_response({"status": "done", "image_urls": ["/image.png"]})

        async def image(request):
            return web.Response(body=b"PNG", content_type="image/png")

        self.server = TestServer(web.Application())
        self.server.app.add_routes(
            [
                web.post("/api/generate", generate),
                web.get("/api/job/job-1", job),
                web.get("/image.png", image),
            ]
        )
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_generate_uses_job_contract(self):
        base = str(self.client.make_url("")).rstrip("/")
        client = AnimaClient(base, model="anima", negative="bad", width=64, height=64, timeout=5)
        with patch("jllm_plugins.imagegen.service.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await client.generate("a small bird"), b"PNG")
        await client.close()
        self.assertEqual(self.payload["model"], "anima")
        self.assertEqual(self.payload["negative"], "bad")
        self.assertEqual(self.payload["safety"], "safe")
        self.assertEqual((self.payload["width"], self.payload["height"]), (64, 64))
        self.assertEqual(self.payload["batch"], 1)

    async def test_missing_job_id_is_an_error(self):
        async def bad(request):
            return web.json_response({})

        self.bad = True
        client = AnimaClient(str(self.client.make_url("")).rstrip("/"), timeout=5)
        with self.assertRaises(ImageGenError):
            await client.generate("bad")
        await client.close()


if __name__ == "__main__":
    unittest.main()
