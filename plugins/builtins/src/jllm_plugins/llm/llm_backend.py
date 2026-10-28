"""翻訳・会話のバックエンドを実行中に切り替える（``/llm switch``）。

``TRANSLATION_BACKEND``（環境変数）は既定値で、いま選ばれているものは
``data/llm_backend.json`` に保存する。bot を再起動しても選択は残る。

同じファイルに**読み上げモード**も持つ:

- ``tts`` … 通常どおり TTS で読み上げる
- ``wav`` … 辞書に登録した wav だけ鳴らし、TTS は使わない

``atmos``（z170 の gemma4-atmos-sft）を選ぶと ``wav`` になる。GPU をモデルが
使っている間に読み上げの合成で取り合わないため。``/llm read`` で単独に変えられる。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

_STATE_PATH_OVERRIDE: str | None = None
_DEFAULT_BACKEND_OVERRIDE: str | None = None


def configure_state(path=None, backend=None):
    global _STATE_PATH_OVERRIDE, _DEFAULT_BACKEND_OVERRIDE
    if path is not None:
        _STATE_PATH_OVERRIDE = str(path)
    if backend is not None:
        _DEFAULT_BACKEND_OVERRIDE = str(backend).strip().lower()


BACKENDS = (
    "sarashina",
    "local",
    "atmos",
)

READ_MODES = ("tts", "wav")


@dataclass(frozen=True)
class BackendInfo:
    """``/llm status`` に出す説明と、切り替えたときの既定の読み上げモード。"""

    display: str
    detail: str
    read_mode: str = "tts"
    #: 起動・停止するローカルプロセスを持つか（``/llm`` の表示と解放の判断に使う）
    manages_process: bool = False


INFO = {
    "sarashina": BackendInfo(
        "Sarashina（クラウド）",
        "文脈128k。VM を使わない・起動停止なし。毎分の上限あり",
        manages_process=False,
    ),
    "local": BackendInfo(
        "ローカル llama.cpp（GPU機）",
        "文脈4096。翻訳対象があるときだけ起動し、無くなると停止して VRAM を返す",
        manages_process=True,
    ),
    "atmos": BackendInfo(
        "gemma4-atmos-sft（z170）",
        "文脈8192・同時4本・約26トークン/秒。#雑談vc の会話で調整した LoRA",
        read_mode="wav",
        manages_process=False,
    ),
}

# 状態ファイルの読み込みは投稿ごとに走るので、mtime が変わったときだけ読み直す。
_cache: dict[str, object] = {"key": None, "data": None}

# いま処理しているチャンネル。asyncio のタスクごとに独立するので、
# 複数チャンネルを同時に処理しても混ざらない。
_channel: contextvars.ContextVar[int | None] = contextvars.ContextVar("llm_channel", default=None)


def current_channel() -> int | None:
    """いま処理中のチャンネル ID（未設定なら None）。"""
    return _channel.get()


def set_channel_context(channel_id):
    """いまのタスクにチャンネルを紐づける。戻り値を :func:`reset_channel_context` に渡す。"""
    return _channel.set(int(channel_id) if channel_id else None)


def reset_channel_context(token) -> None:
    try:
        _channel.reset(token)
    except ValueError, RuntimeError:
        _channel.set(None)


@contextlib.contextmanager
def channel_context(channel_id):
    """このブロックの中だけチャンネル単位の設定を効かせる。"""
    token = _channel.set(int(channel_id) if channel_id else None)
    try:
        yield
    finally:
        _channel.reset(token)


def display_name(backend: str) -> str:
    info = INFO.get(backend)
    return info.display if info else backend


def read_mode_label(mode: str) -> str:
    return "辞書wavのみ（TTSなし）" if mode == "wav" else "TTSで読み上げ"


def default_read_mode(backend: str) -> str:
    info = INFO.get(backend)
    return info.read_mode if info else "tts"


def state_path() -> Path:
    if _STATE_PATH_OVERRIDE:
        return Path(_STATE_PATH_OVERRIDE)
    return Path(os.environ.get("LLM_BACKEND_PATH", "data/llm_backend.json"))


def _default_backend() -> str:
    value = _DEFAULT_BACKEND_OVERRIDE or os.environ.get("TRANSLATION_BACKEND", "")
    value = value.strip().lower()
    return value if value in BACKENDS else "sarashina"


def _read_file(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None

    key = (str(path), stat.st_mtime_ns, stat.st_size)

    if _cache["key"] == key:
        return _cache["data"]  # type: ignore[return-value]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, UnicodeDecodeError:
        data = None

    _cache["key"] = key
    _cache["data"] = data if isinstance(data, dict) else None
    return _cache["data"]  # type: ignore[return-value]


def _forget() -> None:
    _cache["key"] = None
    _cache["data"] = None


def _reset_config() -> None:
    global _STATE_PATH_OVERRIDE, _DEFAULT_BACKEND_OVERRIDE
    _STATE_PATH_OVERRIDE = None
    _DEFAULT_BACKEND_OVERRIDE = None
    _forget()


def _clean_channels(raw) -> dict[int, str]:
    """``{"<channel_id>": "<backend>"}`` を検証して返す。壊れた項目は捨てる。"""

    out: dict[int, str] = {}

    if not isinstance(raw, dict):
        return out

    for key, value in raw.items():
        try:
            channel_id = int(key)
        except TypeError, ValueError:
            continue

        backend = str(value or "").strip().lower()

        if backend in BACKENDS and channel_id > 0:
            out[channel_id] = backend

    return out


def stored() -> dict:
    """保存されている選択（無ければ空）。壊れていても例外にしない。"""

    data = _read_file(state_path()) or {}
    backend = str(data.get("backend") or "").strip().lower()
    mode = str(data.get("read_mode") or "").strip().lower()

    if backend not in BACKENDS:
        # 消えたバックエンドの記録は丸ごと無視する（読み上げモードだけ残さない）
        backend = mode = ""

    if mode not in READ_MODES:
        mode = ""

    return {
        "backend": backend,
        "read_mode": mode,
        "channels": _clean_channels(data.get("channels")),
    }


def snapshot(channel_id=None) -> dict:
    """いまの選択と、その由来（``file`` / ``env`` / ``channel``）。

    ``channel_id`` を渡すと、そのチャンネルに設定があればそれを優先する。
    """

    saved = stored()
    backend = saved["backend"] or _default_backend()
    mode = saved["read_mode"] or default_read_mode(backend)
    channels = saved["channels"]

    if channel_id is None:
        channel_id = current_channel()

    channel_backend = ""

    if channel_id is not None:
        try:
            channel_backend = channels.get(int(channel_id), "")
        except TypeError, ValueError:
            channel_backend = ""

    effective = channel_backend or backend

    if channel_backend:
        mode = default_read_mode(channel_backend)

    return {
        "backend": effective,
        "read_mode": mode,
        "default": _default_backend(),
        "global_backend": backend,
        "channel_id": channel_id if channel_backend else None,
        "channel_backend": channel_backend,
        "channels": dict(channels),
        "source": "channel" if channel_backend else ("file" if saved["backend"] else "env"),
        "display": display_name(effective),
        "info": INFO.get(effective),
    }


def backend_for(channel_id=None) -> str:
    """チャンネル設定 → 全体設定 → 環境既定 の順で解決する。"""
    return snapshot(channel_id)["backend"]


def active_backend() -> str:
    return snapshot()["backend"]


def active_read_mode() -> str:
    return snapshot()["read_mode"]


def set_selection(backend: str | None = None, read_mode: str | None = None) -> dict:
    """保存する。``backend`` を変えて ``read_mode`` を省くと、その既定に合わせる。"""

    current = snapshot()
    target = (backend or current["backend"]).strip().lower()

    if target not in BACKENDS:
        raise ValueError(f"不明なバックエンドです: {backend}")

    if read_mode is None:
        mode = default_read_mode(target) if backend else current["read_mode"]
    else:
        mode = read_mode.strip().lower()

    if mode not in READ_MODES:
        raise ValueError(f"不明な読み上げモードです: {read_mode}")

    path = state_path()
    payload = {
        "version": 1,
        "backend": target,
        "read_mode": mode,
        "channels": {str(k): v for k, v in stored()["channels"].items()},
        "updated": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    _forget()
    return snapshot()


def set_channel(channel_id, backend) -> dict:
    """そのチャンネルだけ別のバックエンドにする。"""

    target = (backend or "").strip().lower()

    if target not in BACKENDS:
        raise ValueError(f"不明なバックエンドです: {backend}")

    try:
        key = int(channel_id)
    except TypeError, ValueError:
        raise ValueError("チャンネル ID が不正です") from None

    if key <= 0:
        raise ValueError("チャンネル ID が不正です")

    saved = stored()
    channels = dict(saved["channels"])
    channels[key] = target

    _write_state(saved, channels)
    return snapshot(key)


def clear_channel(channel_id) -> dict:
    """そのチャンネルの設定を消す（全体設定に戻る）。"""

    try:
        key = int(channel_id)
    except TypeError, ValueError:
        raise ValueError("チャンネル ID が不正です") from None

    saved = stored()
    channels = dict(saved["channels"])
    channels.pop(key, None)

    _write_state(saved, channels)
    return snapshot(key)


def _write_state(saved: dict, channels: dict[int, str]) -> None:
    path = state_path()
    payload = {
        "version": 1,
        "backend": saved["backend"] or _default_backend(),
        "read_mode": saved["read_mode"]
        or default_read_mode(saved["backend"] or _default_backend()),
        "channels": {str(k): v for k, v in channels.items()},
        "updated": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    _forget()
