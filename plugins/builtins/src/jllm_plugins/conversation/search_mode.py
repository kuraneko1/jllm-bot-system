"""検索ツールの ON/OFF 設定（チャンネル別 + 全体既定）。

data/search_tool.json に保存する。読み込みは mtime キャッシュで、
投稿ごとにファイルを読み直さない。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_VERSION = 1


class SearchModeStore:
    def __init__(self, path="data/search_tool.json", default=False):
        self.path = Path(path)
        self.default = bool(default)
        self._cache = (None, None)

    # ------------------------------------------------------------ 読み込み

    def _load(self) -> dict:
        try:
            stat = self.path.stat()
        except OSError:
            return {"default": self.default, "channels": {}}

        key = (stat.st_mtime_ns, stat.st_size)

        if self._cache[0] == key:
            return self._cache[1]

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        channels = {}

        for raw, value in (data.get("channels") or {}).items():
            try:
                channels[int(raw)] = bool(value)
            except (TypeError, ValueError):
                continue

        loaded = {
            "default": bool(data.get("default", self.default)),
            "channels": channels,
        }
        self._cache = (key, loaded)
        return loaded

    def enabled(self, channel_id=None) -> bool:
        data = self._load()

        if channel_id is not None:
            try:
                key = int(channel_id)
            except (TypeError, ValueError):
                key = None

            if key is not None and key in data["channels"]:
                return data["channels"][key]

        return data["default"]

    def snapshot(self, channel_id=None) -> dict:
        data = self._load()
        return {
            "default": data["default"],
            "channels": dict(data["channels"]),
            "enabled": self.enabled(channel_id),
        }

    # ------------------------------------------------------------ 書き込み

    def set(self, channel_id, enabled: bool) -> dict:
        data = self._load()
        channels = dict(data["channels"])
        default = data["default"]

        if channel_id is None:
            default = bool(enabled)
        else:
            channels[int(channel_id)] = bool(enabled)

        self._write(default, channels)
        return self.snapshot(channel_id)

    def clear(self, channel_id) -> dict:
        data = self._load()
        channels = dict(data["channels"])
        channels.pop(int(channel_id), None)
        self._write(data["default"], channels)
        return self.snapshot(channel_id)

    def _write(self, default: bool, channels: dict) -> None:
        payload = {
            "version": STATE_VERSION,
            "default": bool(default),
            "channels": {str(key): bool(value) for key, value in sorted(channels.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.path)
        self._cache = (None, None)
