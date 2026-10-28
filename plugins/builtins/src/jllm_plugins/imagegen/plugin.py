import os

from .control import ImageGenController, NoImageGenController
from .service import AnimaClient


class Plugin:
    async def setup(self, ctx):
        self.client = AnimaClient(
            ctx.config.get("base_url", "http://127.0.0.1:8084"),
            model=ctx.config.get("model", ""),
            negative=ctx.config.get("negative", ""),
            width=ctx.config.get("width", 1024),
            height=ctx.config.get("height", 1024),
            timeout=ctx.config.get("timeout", 300.0),
        )
        ctx.scope.add(self.client.close)
        host = os.environ.get("ANIMA_CONTROL_HOST", "")
        key = os.environ.get("ANIMA_CONTROL_KEY", "")
        known_hosts = os.environ.get("ANIMA_CONTROL_KNOWN_HOSTS", "")
        self.controller = (
            ImageGenController(host, key, known_hosts)
            if all((host, key, known_hosts))
            else NoImageGenController()
        )
        return {"imagegen.provider": self.client, "imagegen.controller": self.controller}

    async def start(self):
        pass

    async def stop(self):
        pass
