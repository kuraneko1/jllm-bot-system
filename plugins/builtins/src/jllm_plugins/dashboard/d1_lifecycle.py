# ruff: noqa: E501
"""ウミガメの出題者 d1-3B の自動解放。

d1（``d1-local.service`` / :8083）はウミガメを遊ぶあいだだけ必要で、VRAM を約4GiB
使う。遊んでいないときに立ちっぱなしだと Irodori（約5.6GiB）と共存できないので、

- ゲーム開始・判定の前に :meth:`D1Lifecycle.ensure` で起こす（既に動いていれば何もしない）
- 最後の利用から ``D1_IDLE_SECONDS``（既定300秒）で :meth:`maybe_stop` が止める

操作は GPU機の ``server/d1-control.py`` を制限付きSSH（強制コマンド）で呼ぶだけで、
Irodori・画像生成・翻訳には触れない。失敗してもゲームは止めない（警告ログのみ）。
"""

import asyncio
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

#: これだけ使われなければ止める（秒）
DEFAULT_IDLE_SECONDS = 300.0


class D1LifecycleError(Exception):
    pass


class NoD1Lifecycle:
    """``D1_CONTROL_*`` が無い環境用（何もしない）。"""

    configured = False

    def manages(self, url):
        return False

    def touch(self):
        pass

    def start_in_background(self):
        pass

    async def ensure(self):
        return False

    async def maybe_stop(self):
        return False

    async def close(self):
        pass


def _http_healthy(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


class D1Controller:
    """GPU機の ``d1-control.py`` を SSH で叩く。"""

    actions = ('start', 'stop', 'status')

    def __init__(self, host, key, known_hosts, *, timeout=120):
        self.host, self.key, self.known_hosts = host, key, known_hosts
        self.timeout = timeout

    async def run(self, action):
        if action not in self.actions:
            raise D1LifecycleError(f"不明な操作です: {action}")

        try:
            process = await asyncio.create_subprocess_exec(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-i", self.key,
                self.host, action,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise D1LifecycleError("d1 の制御に接続できませんでした。") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()

            if isinstance(exc, asyncio.CancelledError):
                raise

            raise D1LifecycleError(f"d1 の操作がタイムアウトしました（{self.timeout}秒）。") from exc

        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:160]
            raise D1LifecycleError(
                "d1 の応答を確認できませんでした" + (f"（{detail}）" if detail else "。")
            ) from exc

        if result.get("ok") is not True:
            raise D1LifecycleError(result.get("error") or "d1 の操作に失敗しました。")

        return result


class D1Lifecycle:
    configured = True

    def __init__(self, controller, *, idle_seconds=DEFAULT_IDLE_SECONDS, health_url="", api_base=""):
        self.controller = controller
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.health_url = health_url
        self.api_base = api_base
        self.last_activity = time.monotonic()
        self._lock = asyncio.Lock()
        self._reaper = None

    def manages(self, url):
        """その URL がこのライフサイクルの担当（設定された D1_API_URL）か。

        テストや別の d1 を指すクライアントのために、勝手に起動しないための番人。
        """

        if not self.api_base or not url:
            return False

        return url.rstrip("/") == self.api_base

    # ------------------------------------------------------------ 利用の記録

    def touch(self):
        """使ったことを記録し、見張りを起動する（イベントループ外では何もしない）。"""

        self.last_activity = time.monotonic()

        if self._reaper is None or self._reaper.done():
            try:
                self._reaper = asyncio.get_running_loop().create_task(self._reap())
            except RuntimeError:
                self._reaper = None

    def start_in_background(self):
        """ゲーム開始時に先回りして起こす（失敗はログだけ）。"""

        self.touch()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(self._safe_ensure())

    async def _safe_ensure(self):
        try:
            await self.ensure()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 ゲームは止めない
            logger.warning("d1 の起動に失敗しました", exc_info=True)

    # ------------------------------------------------------------ 起動・停止

    async def _healthy(self):
        if not self.health_url:
            return False

        return await asyncio.to_thread(_http_healthy, self.health_url)

    async def ensure(self):
        """動いていなければ起こして疎通を待つ（既に動いていれば即 True）。"""

        self.touch()

        if not self.health_url:
            # 疎通先が分からないと起動済みか判定できない（毎回SSHしない）
            return False

        if await self._healthy():
            return True

        async with self._lock:
            # 待っているあいだに他が起こしたかもしれない
            if await self._healthy():
                return True

            try:
                await self.controller.run("start")
            except D1LifecycleError as exc:
                logger.warning("d1 を起動できません: %s", exc)
                return False

            self.touch()
            return await self._healthy()

    async def maybe_stop(self):
        """十分に使われていなければ止める。止めたら True。"""

        idle = time.monotonic() - self.last_activity

        if idle < self.idle_seconds:
            return False

        async with self._lock:
            if time.monotonic() - self.last_activity < self.idle_seconds:
                return False

            try:
                result = await self.controller.run("stop")
            except D1LifecycleError as exc:
                logger.warning("d1 を停止できません: %s", exc)
                return False

            logger.info("d1 を停止しました（%.0f秒アイドル）", idle)

            return bool(result.get("stopped", True))

    async def _reap(self):
        interval = max(5.0, min(60.0, self.idle_seconds / 2))

        while True:
            await asyncio.sleep(interval)

            try:
                if await self.maybe_stop():
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 見張りは落とさない
                logger.warning("d1 の見張りで失敗しました", exc_info=True)

    async def close(self):
        if self._reaper is not None:
            self._reaper.cancel()

            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

            self._reaper = None


def build_d1_lifecycle(env=None):
    """``D1_CONTROL_*`` から作る（未設定なら何もしない実装）。"""

    env = dict(os.environ if env is None else env)
    host = env.get("D1_CONTROL_HOST", "")
    key = env.get("D1_CONTROL_KEY", "")
    known_hosts = env.get("D1_CONTROL_KNOWN_HOSTS", "")

    if not all((host, key, known_hosts)):
        return NoD1Lifecycle()

    return D1Lifecycle(
        D1Controller(host, key, known_hosts),
        idle_seconds=float(env.get("D1_IDLE_SECONDS") or DEFAULT_IDLE_SECONDS),
        health_url=env.get("D1_API_URL", "").rstrip("/") + "/health",
        api_base=env.get("D1_API_URL", "").rstrip("/"),
    )


_default = None


def get():
    """プロセスで1つのライフサイクル（Discord と Web で共有する）。"""

    global _default

    if _default is None:
        _default = build_d1_lifecycle()

    return _default


def reset():
    """テスト用にシングルトンを捨てる。"""

    global _default
    _default = None
