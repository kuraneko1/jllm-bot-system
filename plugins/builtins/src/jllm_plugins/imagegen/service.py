import asyncio

import aiohttp


class ImageGenError(Exception):
    pass


class AnimaClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "",
        negative: str = "",
        width: int = 1024,
        height: int = 1024,
        timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.negative = negative
        self.width = width
        self.height = height
        self.timeout = timeout

        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    async def close(self):
        await self.session.close()

    async def generate(self, prompt: str) -> bytes:
        payload = {
            "prompt": prompt,
            "negative": self.negative,
            "safety": "safe",
            "width": self.width,
            "height": self.height,
            "batch": 1,
        }

        if self.model:
            payload["model"] = self.model

        try:
            async with self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise ImageGenError(f"generate HTTP {response.status}: {body}")

                data = await response.json()

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ImageGenError("画像生成サーバに接続できません") from exc

        job_id = data.get("job_id")

        if not job_id:
            raise ImageGenError("job_id を取得できません")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout

        while True:
            if loop.time() > deadline:
                raise ImageGenError("画像生成がタイムアウトしました")

            await asyncio.sleep(2)

            try:
                async with self.session.get(f"{self.base_url}/api/job/{job_id}") as response:
                    job = await response.json()

            except (TimeoutError, aiohttp.ClientError):
                continue

            status = job.get("status")

            if status in ("error", "cancelled"):
                raise ImageGenError(f"生成に失敗しました: {status}")

            if status == "done":
                urls = job.get("image_urls") or []

                if not urls:
                    raise ImageGenError("画像URLが取得できません")

                async with self.session.get(f"{self.base_url}{urls[0]}") as response:
                    if response.status != 200:
                        raise ImageGenError(f"画像取得 HTTP {response.status}")

                    return await response.read()
