"""複数のTTSバックエンドをまとめ、切替とフォールバックを担う。

``tts-backends.json`` に並べたバックエンドを1つの :class:`TTSBackend` として見せ、
Cog・ダッシュボードからは今までどおり ``bot.tts`` を触るだけで済むようにする。

- **切替**: ``data/tts_state.json`` の ``preferred``（既定は先頭）を使う。
  ダッシュボードや ``/tts`` から変えられる。
- **フォールバック**: 合成・話者一覧が接続エラーになったら、次のバックエンドを試す。
  成功したものへ切り替えて読み上げを止めない（``failover`` が false なら試さない）。
- **自動復帰**: 定期ヘルスチェックで優先バックエンドが戻ったら、自動で戻す。
- **ストリーミング**: active が ``stream()`` を持ち、そのバックエンドの配信方式が
  ``stream`` なら、届いた順に鳴らせるように素通しする。方式（``stream`` / ``wav``）と
  ``failover`` はダッシュボードから変えられ、``tts_state.json`` に残る。
- **空テイク**: :class:`TTSContentError`（エンジンは健全で、この 1 件だけ失敗）は
  不調扱いにせず、使うエンジンも切り替えない（その 1 件だけ他で読む）。

設定ファイルが無いときは、従来どおり ``TTS_BACKEND`` の1つだけを包んで返す
（``build_tts()`` が面倒を見る）。詳細は ``docs/backends.md``。
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from jllm_tts import TTSBackend, TTSBackendError, TTSContentError, Voice

logger = logging.getLogger(__name__)

#: バックエンド種別ごとに、設定JSONのキーを環境変数へ対応づける
KIND_ENV_KEYS: dict[str, dict[str, str]] = {
    "irodori": {
        "url": "TTS_IRODORI_URL",
        "voice": "TTS_IRODORI_VOICE",
        "voice_order": "TTS_IRODORI_VOICE_ORDER",
        "caption": "IRODORI_CAPTION",
        "api_key": "IRODORI_API_KEY",
        "model": "IRODORI_MODEL",
        "timeout": "IRODORI_TIMEOUT",
        "voices_cache": "IRODORI_VOICES_CACHE",
        "lora_map": "IRODORI_LORA_MAP_FILE",
        "disabled_voices": "VOICE_DISABLED_PATH",
    },
    "voicevox": {
        "url": "TTS_VOICEVOX_URL",
        "voice": "TTS_VOICEVOX_SPEAKER",
        "timeout": "VOICEVOX_TIMEOUT",
    },
    "speechcpp": {
        "url": "TTS_SPEECHCPP_URL",
        "voice": "TTS_SPEECHCPP_VOICE",
        "voices_dir": "TTS_SPEECHCPP_VOICES_DIR",
        "model": "SPEECHCPP_MODEL",
        "timeout": "SPEECHCPP_TIMEOUT",
    },
    "openai": {
        "url": "TTS_OPENAI_URL",
        "voice": "TTS_OPENAI_VOICE",
        "voices": "TTS_OPENAI_VOICES",
        "voices_path": "TTS_OPENAI_VOICES_PATH",
        "model": "TTS_OPENAI_MODEL",
        "speed_mode": "TTS_OPENAI_SPEED_MODE",
        "default_speed": "TTS_OPENAI_DEFAULT_SPEED",
        "ignore_speaker_speed": "TTS_OPENAI_IGNORE_SPEED",
        "seed": "TTS_OPENAI_SEED",
        "silence_retries": "TTS_OPENAI_SILENCE_RETRIES",
        "silence_rms_dbfs": "TTS_OPENAI_SILENCE_RMS_DBFS",
        "health_path": "TTS_OPENAI_HEALTH_PATH",
        "timeout": "TTS_OPENAI_TIMEOUT",
    },
}


@dataclass
class BackendSpec:
    """1つのバックエンドの定義（設定ファイルの1要素）。"""

    id: str
    kind: str
    display: str = ""
    options: dict = field(default_factory=dict)

    def env_for(self, base: dict[str, str]) -> dict[str, str]:
        """共通の環境変数に、この定義の上書きを重ねた dict を作る。"""

        env = dict(base)
        mapping = KIND_ENV_KEYS.get(self.kind, {})

        for key, value in self.options.items():
            if key == "env":
                env.update({str(k): str(v) for k, v in (value or {}).items()})
                continue

            if value in (None, ""):
                continue

            name = mapping.get(key)

            if name:
                env[name] = (
                    ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
                )
            else:
                env.setdefault(f"TTS_{self.kind.upper()}_{key.upper()}", str(value))

        return env

    def label(self) -> str:
        return self.display or self.id


def load_specs(path) -> dict:
    """``tts-backends.json`` を読む。無ければ空を返す（＝単一バックエンド運用）。"""

    if not path:
        return {}

    path = Path(path)

    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TTSBackendError(f"TTSバックエンドの設定を読み込めません: {path}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("backends"), list):
        raise TTSBackendError(f"TTSバックエンドの設定形式が不正です: {path}")

    specs: list[BackendSpec] = []
    seen: set[str] = set()

    for item in data["backends"]:
        if not isinstance(item, dict):
            continue

        kind = str(item.get("kind") or item.get("backend") or "").strip().lower()
        ident = str(item.get("id") or kind).strip()

        if not kind or not ident or ident in seen:
            continue

        seen.add(ident)
        specs.append(
            BackendSpec(
                id=ident,
                kind=kind,
                display=str(item.get("display") or ""),
                options={
                    k: v for k, v in item.items() if k not in ("id", "kind", "backend", "display")
                },
            )
        )

    if not specs:
        return {}

    return {
        "specs": specs,
        "failover": bool(data.get("failover", True)),
        "health_seconds": float(data.get("health_seconds") or 30.0),
    }


class TTSRouter(TTSBackend):
    """複数バックエンドの代理。使えたものを ``active`` として覚える。"""

    name = "router"

    def __init__(
        self,
        specs: list[BackendSpec],
        *,
        env: dict[str, str] | None = None,
        state_path=None,
        failover: bool = True,
        health_seconds: float = 30.0,
        builder=None,
        lifecycle=None,
        providers: dict[str, TTSBackend] | None = None,
        disabled_voices_path: str = "",
    ):
        if not specs:
            raise TTSBackendError("TTSバックエンドが1つも設定されていません。")

        self.specs = list(specs)
        # 切替時に「選んだ先以外を落として起動する」係（未設定なら何もしない）
        self.lifecycle = lifecycle
        self._switching = False
        self.env = dict(env or {})
        disabled_path = disabled_voices_path or self.env.get("VOICE_DISABLED_PATH", "")
        self.disabled_voices_path = Path(disabled_path) if disabled_path else None
        self.state_path = Path(state_path) if state_path else None
        self.failover = bool(failover)
        self.health_seconds = max(5.0, float(health_seconds))
        if builder is None:
            if providers is None:
                raise TTSBackendError("backend builder または providers を指定してください。")

            def builder(spec):
                return providers[spec.id]

        self._builder = builder

        self.backends: dict[str, TTSBackend] = {}
        self.unhealthy: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        state = self._load_state()
        known = {spec.id for spec in self.specs}
        self.preferred = (
            state.get("preferred") if state.get("preferred") in known else self.specs[0].id
        )
        self.active = state.get("active") if state.get("active") in known else self.preferred
        # ダッシュボードで変えた配信方式。
        # id → True=stream / False=wav。無ければ各バックエンドの既定。
        streaming = state.get("streaming") if isinstance(state.get("streaming"), dict) else {}
        self.streaming: dict[str, bool] = {
            key: bool(value) for key, value in streaming.items() if key in known
        }
        # ダッシュボードで failover を変えたときだけ設定値より優先する（None = 設定どおり）
        self._failover_override: bool | None = (
            bool(state["failover"]) if isinstance(state.get("failover"), bool) else None
        )

        if self._failover_override is not None:
            self.failover = self._failover_override

        for spec in self.specs:
            try:
                self.backends[spec.id] = self._builder(spec)
            except TTSBackendError as exc:
                logger.warning("TTSバックエンド %s を準備できません: %s", spec.id, exc)
                self.unhealthy[spec.id] = time.time()
                self.last_error[spec.id] = str(exc)

    # ------------------------------------------------------------------ 基本情報

    @property
    def display_name(self) -> str:  # type: ignore[override]
        return self.spec_of(self.active).label()

    def spec_of(self, ident: str) -> BackendSpec:
        return next((spec for spec in self.specs if spec.id == ident), self.specs[0])

    def describe(self) -> str:
        spec = self.spec_of(self.active)
        backend = self.backends.get(self.active)
        detail = backend.describe() if backend else spec.kind

        if self.active != self.preferred:
            return (
                f"router: {spec.label()} に退避中"
                f"（優先: {self.spec_of(self.preferred).label()} / {detail}）"
            )

        return f"router: {spec.label()} ({detail})"

    def status(self) -> dict:
        """ダッシュボード・``/tts`` に見せる現在の状態。"""

        now = time.time()

        return {
            "active": self.active,
            "preferred": self.preferred,
            "failover": self.failover,
            "health_seconds": self.health_seconds,
            "switching": self._switching,
            "lifecycle": bool(getattr(self.lifecycle, "configured", False)),
            "backends": [
                {
                    "id": spec.id,
                    "kind": spec.kind,
                    "display": spec.label(),
                    "url": spec.options.get("url", ""),
                    "active": spec.id == self.active,
                    "preferred": spec.id == self.preferred,
                    "healthy": spec.id not in self.unhealthy,
                    "checked_ago": round(now - self.unhealthy[spec.id], 1)
                    if spec.id in self.unhealthy
                    else None,
                    "error": self.last_error.get(spec.id, ""),
                    "stream_capable": self.stream_capable(spec.id),
                    "stream_mode": "stream" if self.stream_enabled(spec.id) else "wav",
                }
                for spec in self.specs
            ],
        }

    # ------------------------------------------------------------------ 切替

    def _load_state(self) -> dict:
        if self.state_path is None or not self.state_path.is_file():
            return {}

        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}

        return data if isinstance(data, dict) else {}

    def _save_state(self):
        if self.state_path is None:
            return

        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "preferred": self.preferred,
                "active": self.active,
                "unhealthy": {key: round(value, 1) for key, value in self.unhealthy.items()},
                "streaming": dict(self.streaming),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }

            if self._failover_override is not None:
                payload["failover"] = self._failover_override

            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temp, self.state_path)
        except OSError:
            logger.warning("TTSの状態を保存できませんでした: %s", self.state_path)

    async def set_active(self, ident: str, *, preferred: bool = True) -> dict:
        """使うバックエンドを変える（``preferred`` も更新して自動復帰の基準にする）。

        ライフサイクル（``TTS_CONTROL_*``）が設定されていれば、先に
        「選んだ先以外のTTSを止めて、選んだ先を起動」する。起動できなくても
        切替自体は行い、理由を ``lifecycle`` に入れて返す。
        """

        if ident not in self.backends:
            raise TTSBackendError(f"そのバックエンドはありません: {ident}")

        if self._switching or self._lock.locked():
            raise TTSBackendError("いま読み上げエンジンを切り替え中です。少し待ってください。")

        async with self._lock:
            # 起動待ちのあいだ、定期チェックが優先先を別のエンジンへ動かさないようにする
            self._switching = True

            try:
                lifecycle = await self._run_lifecycle(ident)
            finally:
                self._switching = False

            self.active = ident

            if preferred:
                self.preferred = ident

            healthy = await self._probe_after_switch(ident, lifecycle)
            self._save_state()

        return {
            "active": self.active,
            "preferred": self.preferred,
            "healthy": healthy,
            "lifecycle": lifecycle,
        }

    async def _run_lifecycle(self, ident: str) -> dict | None:
        """選んだ先の起動と、他のTTSの停止（失敗しても例外にしない）。"""

        if self.lifecycle is None:
            return None

        try:
            return await self.lifecycle.ensure(ident)
        except Exception:  # noqa: BLE001 何が起きても切替は続ける
            logger.exception("TTSライフサイクルの実行に失敗しました: %s", ident)
            return {
                "configured": bool(getattr(self.lifecycle, "configured", False)),
                "backend": ident,
                "stopped": [],
                "started": False,
                "healthy": False,
                "seconds": 0.0,
                "error": "サーバの起動操作に失敗しました。",
            }

    async def _probe_after_switch(self, ident: str, lifecycle: dict | None) -> bool:
        """切替後の疎通。ホスト側が起動を待っているので、普通は1回で足りる。

        起動した直後の取りこぼしに備えて、起動した場合だけ十数秒まで再試行する。
        """

        healthy = await self.probe(ident)

        if healthy or not lifecycle or not lifecycle.get("started"):
            return healthy

        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)

            if await self.probe(ident):
                return True

        return False

    def set_streaming(self, ident: str, enabled: bool) -> None:
        """そのバックエンドの配信方式を変える（True=stream / False=wav）。"""

        if ident not in self.backends:
            raise TTSBackendError(f"そのバックエンドはありません: {ident}")

        if enabled and not self.stream_capable(ident):
            raise TTSBackendError(f"{ident} はストリーミングに対応していません")

        self.streaming[ident] = bool(enabled)
        self._save_state()

    def set_failover(self, enabled: bool) -> None:
        """自動退避を入れる／切る（設定ファイルより優先して残る）。"""

        self.failover = bool(enabled)
        self._failover_override = self.failover
        self._save_state()

    def _order(self, start: str | None = None) -> list[str]:
        """試す順番。いま使っている先 → 優先 → 残り（定義順）。

        優先先を先に試すと、落ちているあいだ毎回タイムアウトを待つうえ、
        キャッシュで応答しただけの先に戻ってしまう。復帰は定期チェック
        （``check_all``）に任せ、呼び出しは使えている先から当たる。
        """

        order: list[str] = []

        for ident in (start or self.active, self.preferred):
            if ident and ident not in order:
                order.append(ident)

        for spec in self.specs:
            if spec.id not in order:
                order.append(spec.id)

        return [ident for ident in order if ident in self.backends]

    def _first_healthy(self, order: list[str]) -> str | None:
        return next((ident for ident in order if ident not in self.unhealthy), None)

    # ------------------------------------------------------------------ ヘルスチェック

    async def probe(self, ident: str) -> bool:
        """1つのバックエンドに触ってみる。"""

        backend = self.backends.get(ident)

        if backend is None:
            return False

        try:
            await asyncio.wait_for(backend.health(), timeout=15)
        except (TimeoutError, TTSBackendError) as exc:
            self.unhealthy[ident] = time.time()
            self.last_error[ident] = str(exc) or type(exc).__name__
            return False
        except Exception as exc:  # noqa: BLE001 ネットワーク系はまとめて「不調」にする
            self.unhealthy[ident] = time.time()
            self.last_error[ident] = str(exc) or type(exc).__name__
            return False

        self.unhealthy.pop(ident, None)
        self.last_error.pop(ident, None)

        return True

    async def check_all(self) -> dict:
        if self._switching:
            # 切替（起動待ち）の最中に動かすと、起動したばかりの先を「不調」と
            # 誤判定して別のエンジンへ逃げてしまう。終わってから次の周期で見る。
            return self.status()

        for spec in self.specs:
            await self.probe(spec.id)

        if self.active in self.unhealthy:
            # いま使っている先が落ちていたら、使える方へ移す
            fallback = self._first_healthy(self._order())

            if fallback and fallback != self.active:
                logger.info("TTSバックエンドを %s に退避します（%s が不調）", fallback, self.active)
                self.active = fallback
        elif self.active != self.preferred and self.preferred not in self.unhealthy:
            # 優先が戻っていれば自動で戻す
            logger.info("TTSバックエンドを %s に戻します", self.preferred)
            self.active = self.preferred

        self._save_state()

        return self.status()

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(self.health_seconds)
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 監視は落とさない
                logger.exception("TTSバックエンドの監視で失敗")

    def start_watch(self):
        if self._task is None and self.health_seconds > 0 and len(self.specs) > 1:
            self._task = asyncio.create_task(self._watch())

    # ------------------------------------------------------------------ 呼び出し

    async def _call(self, method: str, *args, **kwargs):
        """active で実行し、接続エラーなら他を試す（成功した方を active にする）。"""

        first, *rest = self._order() or [None]
        # 落ちていると分かっている先は後回し（毎回つなぎに行って待たない）。
        # ただし active は不調印が付いていても先頭のまま当たる。
        rest.sort(key=lambda ident: ident in self.unhealthy)
        order = [first, *rest] if first is not None else []
        errors: list[str] = []
        # active が「この 1 件だけ」失敗した（空テイク等）なら、他で読んでも切り替えない
        keep_active = False

        for index, ident in enumerate(order):
            if index and not self.failover:
                break

            backend = self.backends[ident]

            try:
                result = await getattr(backend, method)(*args, **kwargs)
            except TTSContentError as exc:
                if ident == self.active:
                    keep_active = True

                errors.append(f"{ident}: {exc}")
                logger.warning(
                    "TTS %s で %s に失敗（この 1 件のみ・切り替えません）: %s", ident, method, exc
                )
                continue
            except TTSBackendError as exc:
                self.unhealthy[ident] = time.time()
                self.last_error[ident] = str(exc) or type(exc).__name__
                errors.append(f"{ident}: {exc}")
                logger.warning("TTS %s で %s に失敗: %s", ident, method, exc)
                continue

            if keep_active and ident != self.active:
                logger.info("TTS %s で代わりに読みました（%s のまま）", ident, self.active)
                self.unhealthy.pop(ident, None)
                self.last_error.pop(ident, None)
                return result

            if ident != self.active:
                logger.warning(
                    "TTSバックエンドを %s に切り替えました（%s が応答しないため）",
                    ident,
                    self.active,
                )
                self.active = ident

            self.unhealthy.pop(ident, None)
            self.last_error.pop(ident, None)
            self._save_state()

            return result

        raise TTSBackendError(" / ".join(errors) or "利用できるTTSバックエンドがありません。")

    async def setup(self):
        for spec in self.specs:
            backend = self.backends.get(spec.id)

            if backend is None:
                continue

            try:
                await backend.setup()
            except Exception as exc:  # noqa: BLE001 起動時に1つ落ちても続ける
                logger.warning("TTSバックエンド %s の初期化に失敗: %s", spec.id, exc)
                self.unhealthy[spec.id] = time.time()
                self.last_error[spec.id] = str(exc) or type(exc).__name__
                continue

            # 起動時に疎通を確かめて、使える方を active にする
            await self.probe(spec.id)

        if self.preferred in self.unhealthy:
            fallback = self._first_healthy(self._order())

            if fallback:
                self.active = fallback

        self._save_state()
        self.start_watch()

    async def close(self):
        if self._task is not None:
            self._task.cancel()

            try:
                await self._task
            except asyncio.CancelledError, Exception:  # noqa: BLE001
                pass

            self._task = None

        for backend in self.backends.values():
            try:
                await backend.close()
            except Exception:  # noqa: BLE001
                logger.exception("TTSバックエンドの終了処理に失敗")

    async def list_voices(self) -> list[Voice]:
        return await self._call("list_voices")

    async def speakers(self) -> list[dict]:
        return await self._call("speakers")

    async def synthesize(self, text: str, speaker_id: int | str, speed: float = 1.0) -> bytes:
        return await self._call("synthesize", text, speaker_id, speed)

    async def health(self) -> None:
        await self._call("health")

    # --- ストリーミング（active のバックエンドを素通しする） ---

    def stream_capable(self, ident: str) -> bool:
        backend = self.backends.get(ident)

        return bool(getattr(backend, "supports_streaming", False)) and hasattr(backend, "stream")

    def stream_enabled(self, ident: str) -> bool:
        return self.stream_capable(ident) and self.streaming.get(ident, True)

    @property
    def supports_streaming(self) -> bool:
        """いま使っている先で、届いた順に鳴らせるか（プレイヤーが毎回見る）。"""

        return self.stream_enabled(self.active)

    def resolve_speed(self, speed: float | None) -> float:
        """active のバックエンドの速度ポリシー（``ignore_speaker_speed`` 等）に従う。"""

        backend = self.backends.get(self.active)

        if backend is None:
            return super().resolve_speed(speed)

        return backend.resolve_speed(speed)

    async def stream(self, text: str, speaker_id: int | str, speed: float = 1.0):
        """active の ``stream()`` をそのまま流す。

        失敗しても不調扱いにはしない。プレイヤーが wav の経路（:meth:`synthesize`）へ
        戻すので、退避・不調の判断はそちらの ``_call`` に任せる。
        """

        ident = self.active
        backend = self.backends.get(ident)

        if backend is None or not self.stream_enabled(ident):
            raise TTSBackendError(f"{ident} はストリーミングしません")

        async for chunk in backend.stream(text=text, speaker_id=speaker_id, speed=speed):
            yield chunk

    # --- 話者管理（対応しているバックエンドだけが返す） ---

    @property
    def supports_voice_management(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.backends.get(self.active), "supports_voice_management", False))

    def _shared_disabled_voices(self) -> set:
        """無効化した話者名はエンジン共通の1ファイル（``disabled_voices.json``）が正。

        有効なバックエンドが Irodori 以外（DRAMA3 等）でも同じ無効状態を尊重するため、
        active なバックエンドの持ち物だけでなくこのファイルも読む。
        """

        path = self.disabled_voices_path
        if path is None and self.env.get("VOICE_DISABLED_PATH"):
            path = Path(self.env["VOICE_DISABLED_PATH"])
        if path is None:
            return set()

        try:
            data = json.loads(path.read_text("utf-8"))

        except OSError, ValueError:
            return set()

        if not isinstance(data, list):
            return set()

        return {str(name) for name in data if isinstance(name, str)}

    @property
    def disabled_voices(self) -> set:
        names = set(getattr(self.backends.get(self.active), "disabled_voices", set()) or set())
        names |= self._shared_disabled_voices()
        return names

    # --- 全エンジンの話者（Web UI の話者クローンページ用） ---

    def _shared_disabled_path(self) -> Path:
        if self.disabled_voices_path is not None:
            return self.disabled_voices_path
        if self.env.get("VOICE_DISABLED_PATH"):
            return Path(self.env["VOICE_DISABLED_PATH"])
        raise TTSBackendError("共有話者設定の保存先がありません。")

    def save_shared_disabled(self, names, changed=None) -> None:
        """無効な話者名を共通ファイル（``disabled_voices.json``）に書く。

        読込済みのバックエンドの ``disabled_voices`` も同時に更新するので、
        再起動なしで Irodori の一覧・合成にも効く。

        ``changed=(名前, 有効か)`` を渡したときは、その名前だけを各バックエンドの
        集合に足す／抜く。集合を丸ごと置き換えると、そのバックエンドが自前で
        持っている無効話者（共通ファイルに無いもの）まで消えてしまうため。
        """

        path = self._shared_disabled_path()
        names = {str(name) for name in names if str(name).strip()}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(sorted(names), ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)

        except OSError as exc:
            raise TTSBackendError("話者の有効・無効設定を保存できませんでした。") from exc

        for backend in self.backends.values():
            current = getattr(backend, "disabled_voices", None)

            if not isinstance(current, set):
                continue

            if changed is not None:
                name, enabled = changed

                if enabled:
                    current.discard(name)

                else:
                    current.add(name)

            else:
                current.clear()
                current.update(names)

    def set_shared_voice_enabled(self, name, enabled) -> None:
        """エンジンを問わず、名前だけで有効・無効を切り替える。

        標準の ``none``（参照音声なし）だけは外せない（読み上げの受け皿が消える）。
        """

        if not isinstance(name, str) or not name.strip():
            raise TTSBackendError("話者を指定してください。")

        name = name.strip()

        if name.lower() in {"none", "no_ref", "no-ref"}:
            raise TTSBackendError("その話者は有効・無効を変更できません。")

        names = self._shared_disabled_voices()

        if enabled:
            names.discard(name)

        else:
            names.add(name)

        self.save_shared_disabled(names, changed=(name, bool(enabled)))

    async def voice_catalog(self) -> list:
        """全バックエンドの話者を、エンジンごとにまとめて返す。

        ``enabled`` は共通ファイル（``disabled_voices.json``）と各エンジンの
        無効状態の和集合で決める。1つのエンジンが落ちていても他は返す
        （そのエンジンだけ ``error`` を持つ）。
        """

        disabled = self.disabled_voices
        catalog = []

        for spec in self.specs:
            backend = self.backends.get(spec.id)
            entry = {
                "id": spec.id,
                "display": spec.label(),
                "kind": spec.kind,
                "active": spec.id == self.active,
                "manageable": bool(getattr(backend, "supports_voice_management", False)),
                "voices": [],
                "error": "",
            }

            try:
                voices = await backend.list_voices() if backend is not None else []

            except Exception as exc:  # noqa: BLE001 - 1エンジンの失敗で全体を落とさない
                entry["error"] = str(exc) or type(exc).__name__
                voices = []

            seen = set()

            for voice in voices:
                name = getattr(voice, "name", None) or str(voice)

                if not name or name in seen:
                    continue

                seen.add(name)
                entry["voices"].append(
                    {
                        "name": str(name),
                        "id": getattr(voice, "id", name),
                        "enabled": str(name) not in disabled,
                    }
                )

            catalog.append(entry)

        return catalog

    async def voice_library(self):
        return await self._call("voice_library")

    async def add_voice(self, name, audio):
        return await self._call("add_voice", name, audio)

    async def delete_voice(self, name):
        return await self._call("delete_voice", name)

    async def set_voice_enabled(self, name, enabled):
        return await self._call("set_voice_enabled", name, enabled)
