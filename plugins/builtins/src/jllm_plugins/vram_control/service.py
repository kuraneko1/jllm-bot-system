"""GPU機のVRAMを空ける制御（x230 から制限付きSSHで叩く）。

GPU機の ``server/vram-control.py`` に ``free`` / ``restore`` / ``status`` だけを
投げる。キーは強制コマンドで縛ってあり、他の用途には使えない。
"""

import asyncio
import json
import os

#: 空ける / 戻す / 見る の3つだけ
ACTIONS = ("free", "restore", "status")

#: 1回の操作で待つ上限（restore はモデル読み込みがあるので長め）
TIMEOUTS = {"free": 120, "restore": 320, "status": 30}


class VramControlError(Exception):
    pass


class NoVramController:
    """VRAM制御を設定していない環境用。"""

    configured = False

    async def run(self, action):
        raise VramControlError("VRAM開放の設定がありません（VRAM_CONTROL_*）。")

    async def free(self):
        return await self.run("free")

    async def restore(self):
        return await self.run("restore")

    async def status(self):
        return await self.run("status")


def build_vram_controller(env=None):
    """``VRAM_CONTROL_*`` から制御を作る（未設定なら何もしない実装）。"""

    env = dict(os.environ if env is None else env)
    host = env.get("VRAM_CONTROL_HOST", "")
    key = env.get("VRAM_CONTROL_KEY", "")
    known_hosts = env.get("VRAM_CONTROL_KNOWN_HOSTS", "")
    if not all((host, key, known_hosts)):
        return NoVramController()

    return VramController(host, key, known_hosts)


class VramController:
    configured = True

    def __init__(self, host, key, known_hosts):
        self.host, self.key, self.known_hosts = host, key, known_hosts

    async def run(self, action):
        if action not in ACTIONS:
            raise VramControlError("不明な操作です。")

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
            raise VramControlError("GPU機への接続を開始できませんでした。") from exc

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUTS[action])
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()

            if isinstance(exc, asyncio.CancelledError):
                raise

            raise VramControlError("GPU機の操作がタイムアウトしました。") from exc

        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise VramControlError("GPU機の応答を確認できませんでした。") from exc

        if result.get("ok") is not True:
            raise VramControlError(result.get("error", "GPU機の操作に失敗しました。"))

        return result

    async def free(self):
        return await self.run("free")

    async def restore(self):
        return await self.run("restore")

    async def status(self):
        return await self.run("status")


def vram_summary(result: dict) -> str:
    """``{"vram": {...}}`` を1行の日本語にする。"""

    vram = (result or {}).get("vram") or {}

    if not vram:
        return ""

    return "VRAM 使用 {}MiB / 空き {}MiB（全{}MiB）".format(
        vram.get("used_mib", "?"),
        vram.get("free_mib", "?"),
        vram.get("total_mib", "?"),
    )
