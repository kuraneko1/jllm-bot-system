"""TTSサーバのライフサイクル（切替時に選んだ先だけを動かす）。

``/tts switch`` とダッシュボードの切替は、選択を確定したあとにここを通る。

1. 選んだバックエンド以外のホストの TTS を **全部止める**
2. 選んだホストで ``switch <unit>`` を実行する（ホスト側が他のTTSを止めて起動する）
3. ホスト側が最大60秒まで疎通をポーリングし、起動できたかを返す

管理スクリプトを置けない外部ホスト（:data:`UNMANAGED_HOSTS`。いまは nuko）は 2 を
**no-op** にして 1 だけを行い、ログに「管理対象外」と出す（切替をエラーにしない）。

実際の操作は各ホストの ``server/tts-lifecycle.py``（許可リスト制・``systemctl --user``）
だけが行う。x230 からは制限付きSSHキー（強制コマンド）で呼ぶので、このモジュールから
``d1-local`` や画像生成・翻訳に触ることはできない。

失敗（SSH不能・タイムアウト・unit の失敗）は例外にせず結果に残す。切替そのものは
続ける（選んだ先が優先先になる）。呼び出し側は :func:`switch_message` で短く伝える。
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

#: バックエンド id → (ホスト名, ホスト側の TTS id)。
#: ``None`` は「このホストでは起動しない」（x230 内蔵の VOICEVOX。Docker 側の管理）。
TARGETS = {
    "speechcpp": ("gpu", "speechcpp"),
    "home": ("gpu", "irodori"),
    "home-drama3": ("gpu", "drama3"),
    "ming": ("gpu", "ming"),
    "z170": ("z170", "speechcpp"),
    "z170-ts": ("z170", "speechcpp"),
    "z170-irodori": ("z170", "irodori"),
    # nuko は他人の Tailscale ノード（別テイルネット）。管理スクリプトが無いので
    # 起動・停止はできないが、「他を止める」対象としては数える。
    "nuko": ("nuko", "drama3"),
    "x230-voicevox": None,
}

#: 起動はしないが「他を止める」対象になるバックエンド（x230 内蔵）
LOCAL_ONLY = {"x230-voicevox"}

#: 管理スクリプトが無い（``TTS_CONTROL_*`` を配れない）外部ホスト。
#: 切替のときは「他のTTSを止める」だけ行い、この先の起動操作は **no-op** にする
#: （エラーにもしない）。``TTS_CONTROL_*`` が未設定のまま切替を頼まれると
#: 「起動制御が未設定です」で止まって他を止め損ねるので、その手前で分ける。
UNMANAGED_HOSTS = {"nuko"}

#: ホストごとの SSH タイムアウト（秒）。switch は停止＋起動待ちを含む。
TIMEOUTS = {"status": 30, "stop": 90, "start": 120, "switch": 180}


class LifecycleError(Exception):
    pass


class NoTTSLifecycle:
    """``TTS_CONTROL_*`` が無い環境用（従来どおり何もしない）。"""

    configured = False

    async def ensure(self, ident):
        return None


def target_for(ident):
    """バックエンド id から (ホスト, TTS id) を引く。未登録なら ``KeyError``。"""

    return TARGETS[ident]


class SSHHostController:
    """1ホストの ``tts-lifecycle.py`` を SSH で叩く。"""

    def __init__(self, name, host, key, known_hosts, *, timeout=None):
        self.name = name
        self.host = host
        self.key = key
        self.known_hosts = known_hosts
        self.timeout = timeout

    async def run(self, command):
        timeout = self.timeout or TIMEOUTS.get(command.split()[0], 60)

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
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise LifecycleError(f"{self.name} への接続を開始できませんでした。") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()

            if isinstance(exc, asyncio.CancelledError):
                raise

            raise LifecycleError(
                f"{self.name} の操作がタイムアウトしました（{timeout}秒）。"
            ) from exc

        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:160]
            raise LifecycleError(
                f"{self.name} の応答を確認できませんでした" + (f"（{detail}）" if detail else "。")
            ) from exc

        if result.get("ok") is not True:
            raise LifecycleError(result.get("error") or f"{self.name} の操作に失敗しました。")

        return result


class TTSLifecycle:
    """ホストをまたいで「選んだ先以外を落とし、選んだ先を起動する」。"""

    configured = True

    def __init__(self, hosts):
        self.hosts = dict(hosts)

    async def ensure(self, ident):
        """選択先を起動し、それ以外の TTS を止める。

        戻り値は必ず dict（失敗しても例外にしない）。``healthy`` が起動・疎通の結果。
        """

        result = {
            "configured": True,
            "backend": ident,
            "target": None,
            "stopped": [],
            "started": False,
            "healthy": False,
            "seconds": 0.0,
            "error": "",
            "warnings": [],
            "unmanaged": False,
            "host": "",
        }

        if ident not in TARGETS:
            result["error"] = "このバックエンドの起動先が未登録です。"
            return result

        target = TARGETS[ident]
        result["target"] = list(target) if target else None

        if target and target[0] in UNMANAGED_HOSTS:
            # 管理スクリプトが無い外部ホスト。起動はできないが、切替は妨げない
            # （他のTTSは従来どおり止める）。エラーにはしない。
            logger.info(
                "TTSライフサイクル: %s は管理対象外（%s）なので起動操作をしません",
                ident,
                target[0],
            )
            result["unmanaged"] = True
            result["host"] = target[0]
            # 起動操作はしていないので「操作した先」は無い
            result["target"] = None
            target = None

        elif target and target[0] not in self.hosts:
            # 起動できないホストのために、他のホストを止めてしまうのは避ける
            result["error"] = f"{target[0]} の起動制御が未設定です（TTS_CONTROL_*）。"
            return result

        jobs = []

        for name, host in self.hosts.items():
            if target and name == target[0]:
                continue

            jobs.append(self._stop_all(name, host, result))

        if target:
            jobs.append(self._switch(*target, result))

        await asyncio.gather(*jobs)

        return result

    async def _stop_all(self, name, host, result):
        """そのホストの TTS を全部止める（操作できるのは許可リストだけ）。"""

        try:
            payload = await host.run("stop all")
        except LifecycleError as exc:
            logger.warning("TTSライフサイクル: %s のTTSを止められません: %s", name, exc)
            result["warnings"].append(f"{name} のTTSを止められません: {exc}")
            return

        for ident in payload.get("stopped") or []:
            result["stopped"].append(f"{name}:{ident}")

    async def _switch(self, name, ident, result):
        host = self.hosts[name]

        try:
            payload = await host.run(f"switch {ident}")
        except LifecycleError as exc:
            logger.warning("TTSライフサイクル: %s の %s を起動できません: %s", name, ident, exc)
            result["error"] = f"{name}: {exc}"
            return

        for stopped in payload.get("stopped") or []:
            result["stopped"].append(f"{name}:{stopped}")

        result["started"] = bool(payload.get("started"))
        result["healthy"] = bool(payload.get("healthy"))
        result["seconds"] = float(payload.get("seconds") or 0.0)

        if payload.get("error"):
            detail = payload.get("error")

            if payload.get("log"):
                detail = f"{detail} / ログ: {payload['log']}"

            result["error"] = f"{name}: {detail}"

        if result["healthy"]:
            logger.info("TTSライフサイクル: %s:%s を起動（%.1f秒）", name, ident, result["seconds"])


def build_tts_lifecycle(env=None):
    """``TTS_CONTROL_*``（GPU機）と ``TTS_CONTROL_Z170_*`` から作る。"""

    env = dict(os.environ if env is None else env)
    hosts = {}

    for name, prefix in (("gpu", "TTS_CONTROL"), ("z170", "TTS_CONTROL_Z170")):
        host, key, known = (
            env.get(f"{prefix}_HOST", ""),
            env.get(f"{prefix}_KEY", ""),
            env.get(f"{prefix}_KNOWN_HOSTS", ""),
        )

        if all((host, key, known)):
            hosts[name] = SSHHostController(name, host, key, known)

    if not hosts:
        return NoTTSLifecycle()

    return TTSLifecycle(hosts)


def _stopped_note(stopped):
    if not stopped:
        return ""

    return f"他{len(stopped)}件を停止・"


def switch_message(result, describe, *, healthy=None):
    """切替の結果を1文にする（Discord・ダッシュボード共通）。"""

    result = result or {}
    lifecycle = result.get("lifecycle")

    if healthy is None:
        healthy = result.get("healthy")

    if not lifecycle or not lifecycle.get("configured"):
        return (
            f"読み上げエンジンを {describe} にしました"
            f"（{'疎通OK' if healthy else '疎通できませんでした'}）。"
        )

    stopped = _stopped_note(lifecycle.get("stopped"))
    seconds = lifecycle.get("seconds") or 0.0
    error = lifecycle.get("error") or ""
    warnings = lifecycle.get("warnings") or []
    # 管理スクリプトが無い外部ホスト（nuko）は、起動操作をしていないことを明示する
    unmanaged = bool(lifecycle.get("unmanaged"))
    where = lifecycle.get("host") or "外部ホスト"

    if healthy:
        if unmanaged:
            text = (
                f"読み上げエンジンを {describe} に切り替え、{stopped}疎通を確認しました"
                f"（{where} は管理対象外なので起動操作はしていません）。"
            )
        else:
            note = f"{stopped}起動して疎通を確認しました"

            if lifecycle.get("started") and seconds:
                note += f"（{seconds:.0f}秒）"

            text = f"読み上げエンジンを {describe} に切り替え、{note}。"
    else:
        if error:
            detail = error
        elif unmanaged:
            detail = "サーバに繋がりませんでした（管理対象外なので起動操作はしていません）"
        else:
            detail = "起動しても応答がありませんでした"

        if len(detail) > 300:
            detail = detail[:297] + "…"

        if unmanaged:
            text = (
                f"読み上げエンジンを {describe} に切り替えましたが、"
                f"疎通を確認できません（{detail}）。"
            )
        else:
            text = (
                f"読み上げエンジンを {describe} に切り替えましたが、"
                f"起動・疎通を確認できません（{detail}）。"
            )

    if warnings:
        text += " " + " / ".join(warnings)[:300]

    return text
