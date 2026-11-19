# ruff: noqa: E501
"""Haru-4B 用の人物設定（ペルソナ）とリファレンス音声の管理。"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

MAX_HARU_VOICE_BYTES = 50 * 1024 * 1024
HARU_VOICE_EXTENSIONS = {".wav", ".flac", ".ogg", ".opus", ".mp3", ".webm", ".mp4", ".m4a"}
MIN_REFERENCE_SECONDS = 3.0
MAX_REFERENCE_SECONDS = 180.0
MAX_SYSTEM_PROMPT_CHARS = 4000

GENDER_LABELS = {"female": "女性", "male": "男性"}
BACKCHANNEL_LABELS = {"low": "少なめ", "mid": "普通", "high": "多め"}


def normalize_haru_voice(data: bytes, suffix: str) -> tuple[bytes, float]:
    """任意の音声バイト列を Haru 参照音声（モノラル WAV・3〜180秒・-16 LUFS）へ整える。"""

    if not data:
        raise ValueError("音声ファイルが空です。")
    if len(data) > MAX_HARU_VOICE_BYTES:
        raise ValueError("音声ファイルが大きすぎます（50MB 以内にしてください）。")
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    if ext.lower() not in HARU_VOICE_EXTENSIONS:
        ext = ".wav"

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("音声の変換がタイムアウトしました。短い音声でお試しください。") from exc
        if proc.returncode:
            raise ValueError("音声ファイルを読み込めません。別の音声をお試しください。")
        return proc

    def measure(path: Path) -> dict:
        proc = run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-vn",
                "-af",
                "aformat=channel_layouts=mono,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ]
        )
        return json.JSONDecoder().raw_decode(proc.stderr[proc.stderr.rfind("{") :])[0]

    with tempfile.TemporaryDirectory(prefix="haru-voice-") as directory:
        root = Path(directory)
        source = root / f"upload{ext}"
        decoded = root / "decoded.wav"
        output = root / "reference.wav"
        source.write_bytes(data)

        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-t",
                str(int(MAX_REFERENCE_SECONDS) + 1),
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(decoded),
            ]
        )
        with wave.open(str(decoded), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()

        if duration < MIN_REFERENCE_SECONDS or duration > MAX_REFERENCE_SECONDS:
            raise ValueError(
                f"参照音声は {MIN_REFERENCE_SECONDS:.0f}〜{MAX_REFERENCE_SECONDS:.0f} 秒にしてください"
                f"（現在 {duration:.1f} 秒）。"
            )

        gain = -16.0 - float(measure(decoded)["input_i"])
        if not math.isfinite(gain):
            raise ValueError("声を含む録音を選択してください（無音は登録できません）。")

        for _ in range(10):
            filters = (
                f"aresample=192000,volume={gain}dB,"
                "alimiter=limit=0.82224265:level=false:latency=true:attack=5:release=50,"
                "aresample=48000"
            )
            run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(decoded),
                    "-af",
                    filters,
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ]
            )
            stats = measure(output)
            loudness, peak = float(stats["input_i"]), float(stats["input_tp"])
            if abs(loudness + 16.0) <= 0.15 and peak <= -1.5:
                return output.read_bytes(), round(duration, 2)
            gain += -16.0 - loudness

        return decoded.read_bytes(), round(duration, 2)


def build_system_prompt(persona: dict, *, speaker_name: str = "") -> str:
    """Haru-4B 推奨日本語カード形式で ``system_prompt`` を生成する。"""

    custom = str(persona.get("custom_prompt") or "").strip()
    if custom:
        return custom[:MAX_SYSTEM_PROMPT_CHARS]

    lines = ["あなたは以下の人物として、相手と音声で会話します。"]
    sections = [
        ("【あなたの人物設定】", str(persona.get("persona") or "").strip()),
        ("【あなたの名前】", str(persona.get("name") or "").strip()),
        ("【あなたの性別】", GENDER_LABELS.get(str(persona.get("gender") or ""), "")),
        ("【あなたの相づち】", BACKCHANNEL_LABELS.get(str(persona.get("backchannel") or ""), "")),
        (
            "【相手の名前】",
            str(persona.get("other_name") or "").strip() or speaker_name.strip(),
        ),
        ("【関係性】", str(persona.get("relationship") or "").strip()),
        ("【状況】", str(persona.get("situation") or "").strip()),
    ]
    for header, value in sections:
        if value:
            lines.append(header)
            lines.append(value)

    if len(lines) == 1:
        lines.extend(["【あなたの名前】", "ハル"])
    return "\n".join(lines)[:MAX_SYSTEM_PROMPT_CHARS]


def _clamp_guidance(value) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 1.5
    if not math.isfinite(num):
        return 1.5
    return round(min(2.0, max(1.0, num)), 2)


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower()
    return cleaned[:24] or "persona"


class HaruPersonaStore:
    """話せるひと（キャラクター設定＋参照音声）とギルド別フォーカス設定を永続化する。"""

    def __init__(self, personas_path: Path, voices_dir: Path, focus_path: Path):
        self.personas_path = personas_path
        self.voices_dir = voices_dir
        self.focus_path = focus_path
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self._personas: list[dict] = self._load_personas()
        self._focus: dict[str, int | None] = self._load_focus()

    def _load_personas(self) -> list[dict]:
        if not self.personas_path.is_file():
            return []
        try:
            raw = json.loads(self.personas_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                continue
            result.append(
                {
                    "id": str(item["id"]).strip(),
                    "name": str(item.get("name") or item["id"]).strip(),
                    "persona": str(item.get("persona") or ""),
                    "gender": str(item.get("gender") or "")
                    if item.get("gender") in ("", "female", "male")
                    else "",
                    "backchannel": str(item.get("backchannel") or "mid")
                    if item.get("backchannel") in ("", "low", "mid", "high")
                    else "mid",
                    "other_name": str(item.get("other_name") or ""),
                    "relationship": str(item.get("relationship") or ""),
                    "situation": str(item.get("situation") or ""),
                    "custom_prompt": str(item.get("custom_prompt") or ""),
                    "guidance_scale": _clamp_guidance(item.get("guidance_scale", 1.5)),
                    "voice_file": Path(str(item.get("voice_file") or "")).name,
                    "voice_seconds": float(item.get("voice_seconds") or 0.0),
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        return result

    def _save_personas(self) -> None:
        _atomic_write_json(self.personas_path, self._personas)

    def _load_focus(self) -> dict[str, int | None]:
        if not self.focus_path.is_file():
            return {}
        try:
            raw = json.loads(self.focus_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int | None] = {}
        for k, v in raw.items():
            if v in (None, "", "all"):
                result[str(k)] = None
            else:
                try:
                    result[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return result

    def _save_focus(self) -> None:
        payload = {k: (str(v) if v is not None else None) for k, v in self._focus.items()}
        _atomic_write_json(self.focus_path, payload)

    def list_personas(self) -> list[dict]:
        return [dict(item) for item in self._personas]

    def get(self, persona_id_or_name: str) -> dict | None:
        needle = persona_id_or_name.strip()
        if not needle:
            return None
        for item in self._personas:
            if item["id"] == needle:
                return dict(item)
        low = needle.lower()
        for item in self._personas:
            if item["name"].lower() == low:
                return dict(item)
        return None

    def voice_path(self, persona: dict) -> Path | None:
        name = Path(str(persona.get("voice_file") or "")).name
        if not name:
            return None
        path = self.voices_dir / name
        return path if path.is_file() else None

    def save_persona(
        self,
        *,
        persona_id: str | None = None,
        name: str,
        persona: str = "",
        gender: str = "",
        backchannel: str = "mid",
        other_name: str = "",
        relationship: str = "",
        situation: str = "",
        custom_prompt: str = "",
        guidance_scale: float = 1.5,
        voice_wav: bytes | None = None,
        voice_seconds: float | None = None,
    ) -> dict:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("名前を入力してください。")
        if len(clean_name) > 80:
            raise ValueError("名前は80文字以内にしてください。")
        if gender not in ("", "female", "male"):
            raise ValueError("性別の指定が不正です。")
        if backchannel not in ("", "low", "mid", "high"):
            raise ValueError("相づちの指定が不正です。")

        existing = self.get(persona_id) if persona_id else None
        if persona_id and existing is None:
            raise KeyError("指定された話者が見つかりません。")

        target_id = existing["id"] if existing else f"{_slug(clean_name)}-{uuid.uuid4().hex[:6]}"
        voice_file = existing["voice_file"] if existing else ""
        seconds = float(existing["voice_seconds"]) if existing else 0.0

        if voice_wav is not None:
            filename = f"{target_id}.wav"
            dest = self.voices_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".wav.tmp")
            tmp.write_bytes(voice_wav)
            os.replace(tmp, dest)
            voice_file = filename
            seconds = float(voice_seconds or 0.0)

        record = {
            "id": target_id,
            "name": clean_name,
            "persona": persona.strip(),
            "gender": gender,
            "backchannel": backchannel,
            "other_name": other_name.strip(),
            "relationship": relationship.strip(),
            "situation": situation.strip(),
            "custom_prompt": custom_prompt.strip()[:MAX_SYSTEM_PROMPT_CHARS],
            "guidance_scale": _clamp_guidance(guidance_scale),
            "voice_file": voice_file,
            "voice_seconds": round(seconds, 2),
            "updated_at": datetime.now(UTC).isoformat(),
        }

        if existing is not None:
            self._personas = [record if item["id"] == target_id else item for item in self._personas]
        else:
            self._personas.append(record)
        self._save_personas()
        return dict(record)

    def delete_persona(self, persona_id: str) -> bool:
        target = self.get(persona_id)
        if target is None:
            return False
        self._personas = [item for item in self._personas if item["id"] != target["id"]]
        self._save_personas()
        vpath = self.voice_path(target)
        if vpath is not None:
            vpath.unlink(missing_ok=True)
        return True

    def get_focus(self, guild_id: int | str) -> int | None:
        return self._focus.get(str(guild_id))

    def set_focus(self, guild_id: int | str, user_id: int | None) -> int | None:
        self._focus[str(guild_id)] = int(user_id) if user_id is not None else None
        self._save_focus()
        return self._focus[str(guild_id)]
