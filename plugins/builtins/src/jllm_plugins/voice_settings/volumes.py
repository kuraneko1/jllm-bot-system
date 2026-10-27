"""Per-voice TTS playback volume (Discord-side gain, 0.0-3.0)."""

import json
import math
import os
from pathlib import Path

MAX_VOICE_VOLUME = 3.0


def check_volume(value) -> float:
    """Validate a voice volume, returning it as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("音量は0〜300%の数値で指定してください。")
    gain = float(value)
    if not math.isfinite(gain) or not 0.0 <= gain <= MAX_VOICE_VOLUME:
        raise ValueError("音量は0〜300%の数値で指定してください。")
    return gain


class VoiceVolumes:
    """話者名 → Discord再生ゲイン。WebUIの話者音量スライダーをDiscordへ反映する。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.volumes: dict[str, float] = {}
        self._load()

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return
        if not isinstance(data, dict):
            return
        for name, gain in data.items():
            try:
                self.volumes[str(name)] = check_volume(gain)
            except ValueError:
                continue

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.volumes, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def get(self, name, default: float = 1.0) -> float:
        try:
            return check_volume(self.volumes.get(str(name), default))
        except ValueError:
            return default

    def set(self, name, gain) -> float:
        gain = check_volume(gain)
        self.volumes[str(name)] = gain
        self.save()
        return gain

    def remove(self, name) -> bool:
        if str(name) in self.volumes:
            del self.volumes[str(name)]
            self.save()
            return True
        return False
