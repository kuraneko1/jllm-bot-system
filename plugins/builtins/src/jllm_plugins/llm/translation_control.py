import asyncio
import json
import os


class TranslationControlError(Exception):
    pass


class NoTranslationController:
    """起動・停止するローカルサーバーが無いバックエンド（Sarashina / atmos / 未知）用。

    常に「動いている」扱いにして、VRAM/RAM の解放処理も呼ばない。
    """

    manages_process = False

    async def set_enabled(self, enabled):
        return True


def build_translation_controller(backend=None, config=None):
    """選ばれているバックエンドに応じた制御を作る。

    ``backend`` を省くと ``/llm switch`` の選択（無ければ ``TRANSLATION_BACKEND``）。
    """
    from .llm_backend import active_backend

    config = dict(os.environ if config is None else config)
    host = config.get("TRANSLATION_CONTROL_HOST", "")
    key = config.get("TRANSLATION_CONTROL_KEY", "")
    known_hosts = config.get("TRANSLATION_CONTROL_KNOWN_HOSTS", "")

    if backend is None:
        backend = active_backend()

    if (backend or "").strip().lower() != "local":
        return NoTranslationController()

    return TranslationController(host, key, known_hosts)


class TranslationController:
    manages_process = True

    def __init__(self, host, key, known_hosts):
        self.host, self.key, self.known_hosts = host, key, known_hosts

    async def set_enabled(self, enabled):
        if not all((self.host, self.key, self.known_hosts)):
            raise TranslationControlError("翻訳サーバーの起動・停止設定がありません。")
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
                "start" if enabled else "stop",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TranslationControlError("翻訳サーバーへの接続を開始できませんでした。") from exc
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=100)
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TranslationControlError(
                "翻訳サーバーの起動・停止がタイムアウトしました。"
            ) from exc
        if process.returncode:
            raise TranslationControlError("翻訳サーバーへの接続または操作に失敗しました。")
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise TranslationControlError("翻訳サーバーの応答を確認できませんでした。") from exc
        if result.get("ok") is not True:
            raise TranslationControlError(result.get("error", "翻訳サーバーの操作に失敗しました。"))
        if result.get("running") is not enabled:
            raise TranslationControlError("翻訳サーバーが要求した状態になりませんでした。")
