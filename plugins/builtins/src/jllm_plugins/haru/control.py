"""Haru サーバの起動・停止（VRAM開放）・状態確認（x230 から制限付きSSHで呼ぶ）。"""

from __future__ import annotations

import asyncio
import json
import os

ACTIONS = ("start", "stop", "status")
TIMEOUTS = {"start": 195, "stop": 45, "status": 25}


class HaruControlError(Exception):
    pass


class NoHaruController:
    """Haru 起動/停止制御が未設定の環境用。"""

    configured = False

    async def run(self, action: str) -> dict:
        raise HaruControlError("Haru の起動・停止設定がありません（HARU_CONTROL_*）。")

    async def start(self) -> dict:
        return await self.run("start")

    async def stop(self) -> dict:
        return await self.run("stop")

    async def status(self) -> dict:
        return await self.run("status")


def build_haru_controller(env=None):
    """``HARU_CONTROL_*`` から制御を作る（未設定なら :class:`NoHaruController`）。"""

    data = dict(os.environ if env is None else env)
    host = data.get("HARU_CONTROL_HOST", "")
    key = data.get("HARU_CONTROL_KEY", "")
    known_hosts = data.get("HARU_CONTROL_KNOWN_HOSTS", "")
    if not all((host, key, known_hosts)):
        return NoHaruController()
    return HaruController(host, key, known_hosts)


class HaruController:
    configured = True

    def __init__(self, host: str, key: str, known_hosts: str):
        self.host = host
        self.key = key
        self.known_hosts = known_hosts
        self.lock = asyncio.Lock()

    async def run(self, action: str) -> dict:
        if action not in ACTIONS:
            raise HaruControlError("不明な操作です。")
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
            raise HaruControlError("GPU機への接続を開始できませんでした。") from exc

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUTS[action])
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HaruControlError("Haru サーバの操作がタイムアウトしました。") from exc

        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HaruControlError("GPU機の応答を確認できませんでした。") from exc

        if result.get("ok") is not True:
            raise HaruControlError(result.get("error", "Haru サーバの操作に失敗しました。"))
        return result

    async def start(self) -> dict:
        async with self.lock:
            return await self.run("start")

    async def stop(self) -> dict:
        async with self.lock:
            return await self.run("stop")

    async def status(self) -> dict:
        return await self.run("status")
