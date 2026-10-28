import asyncio
import json

from .service import ImageGenError


class ImageGenController:
    def __init__(self, host: str, key: str, known_hosts: str):
        self.host = host
        self.key = key
        self.known_hosts = known_hosts
        self.lock = asyncio.Lock()

    async def run(self, action: str) -> dict:
        if not self.host or not self.key or not self.known_hosts:
            raise ImageGenError("画像生成サーバの起動・停止設定がありません")
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"UserKnownHostsFile={self.known_hosts}",
                "-i",
                self.key,
                self.host,
                action,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ImageGenError("画像生成サーバの操作を開始できませんでした") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=125)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ImageGenError("画像生成サーバの操作がタイムアウトしました") from exc
        if process.returncode:
            raise ImageGenError(
                "画像生成サーバの操作に失敗しました: " + stderr.decode(errors="replace")[-500:]
            )
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ImageGenError("画像生成サーバの応答を確認できませんでした") from exc

    async def set_enabled(self, enabled: bool):
        status = await self.run("start" if enabled else "stop")
        states = list(status.get("services", {}).values())
        if not states or any(state is not enabled for state in states):
            raise ImageGenError("画像生成サーバが要求した状態になりませんでした")


class NoImageGenController:
    configured = False

    def __init__(self):
        self.lock = asyncio.Lock()

    async def set_enabled(self, enabled):
        raise ImageGenError("画像生成サーバの起動・停止設定がありません")
