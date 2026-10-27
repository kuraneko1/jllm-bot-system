"""Per-voice TTS speed (Discord-side default, 0.5-2.0)."""

import json
import math
import os
from pathlib import Path

MIN_VOICE_SPEED = 0.5
MAX_VOICE_SPEED = 2.0


def check_speed(value) -> float:
    """Validate a voice speed, returning it as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("速度は0.5〜2.0で指定してください。")
    speed = float(value)
    if not math.isfinite(speed) or not MIN_VOICE_SPEED <= speed <= MAX_VOICE_SPEED:
        raise ValueError("速度は0.5〜2.0で指定してください。")
    return speed


class VoiceSpeeds:
    """話者名 → Discord読み上げ速度。未設定は None（既定速度を使う）。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.speeds: dict[str, float] = {}
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
        for name, speed in data.items():
            try:
                self.speeds[str(name)] = check_speed(speed)
            except ValueError:
                continue

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.speeds, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def get(self, name) -> float | None:
        try:
            return check_speed(self.speeds[str(name)])
        except KeyError, ValueError:
            return None

    def set(self, name, speed) -> float:
        speed = check_speed(speed)
        self.speeds[str(name)] = speed
        self.save()
        return speed

    def remove(self, name) -> bool:
        if str(name) in self.speeds:
            del self.speeds[str(name)]
            self.save()
            return True
        return False
