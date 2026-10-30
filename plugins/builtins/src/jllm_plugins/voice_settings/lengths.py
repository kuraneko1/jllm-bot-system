"""話者ごとの読み上げ文字数（先頭何文字まで読むか）。"""

import json
import os
from pathlib import Path

MIN_VOICE_LENGTH = 1
MAX_VOICE_LENGTH = 1900
LENGTH_ERROR = "読み上げ文字数は1〜1900の整数で指定してください。"


def check_length(value) -> int:
    """読み上げ文字数を検証して int で返す。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(LENGTH_ERROR)
    if not MIN_VOICE_LENGTH <= value <= MAX_VOICE_LENGTH:
        raise ValueError(LENGTH_ERROR)
    return value


class VoiceLengths:
    """話者名 → 先頭何文字まで読み上げるか。未設定は None（既定値を使う）。

    速度・音量と同じく「声ごとの設定」として持ち、Bot の声も 1 つの話者として
    名前で引く。保存は JSON（話者名 → 文字数）。
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lengths: dict[str, int] = {}
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
        for name, length in data.items():
            try:
                self.lengths[str(name)] = check_length(length)
            except ValueError:
                continue

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.lengths, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def get(self, name) -> int | None:
        try:
            return check_length(self.lengths[str(name)])
        except KeyError, ValueError:
            return None

    def set(self, name, length) -> int:
        length = check_length(length)
        self.lengths[str(name)] = length
        self.save()
        return length

    def remove(self, name) -> bool:
        if str(name) in self.lengths:
            del self.lengths[str(name)]
            self.save()
            return True
        return False
