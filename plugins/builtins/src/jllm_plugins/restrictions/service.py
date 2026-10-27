import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FEATURES = ("wav", "gif", "ignore")
LABELS = {"wav": "wav再生", "gif": "GIF再生", "ignore": "まるごと無視"}
DESCRIPTIONS = {
    "wav": "登録wavを鳴らさず、その語句をTTSで読み上げます",
    "gif": "GIF（メディア）を送りません",
    "ignore": "読み上げ・会話・メディア・引用画像のすべてを無視します",
}


def user_key(guild_id, user_id) -> str:
    return f"{int(guild_id)}:{int(user_id)}"


class UserRestrictions:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries: dict[str, set[str]] = {}
        self._load()

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            logger.warning("ユーザー制限を読み込めませんでした: %s", self.path)
            return
        if not isinstance(data, dict):
            return
        for key, features in data.items():
            if not isinstance(features, list):
                continue
            valid = {str(feature) for feature in features if str(feature) in FEATURES}
            if valid:
                self.entries[str(key)] = valid

    def save(self):
        if self.path is None:
            return
        try:
            payload = {
                key: [feature for feature in FEATURES if feature in features]
                for key, features in sorted(self.entries.items())
                if features
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            logger.warning("ユーザー制限を保存できませんでした: %s", self.path)

    def flags(self, guild_id, user_id) -> frozenset[str]:
        return frozenset(self.entries.get(user_key(guild_id, user_id), ()))

    def blocked(self, guild_id, user_id, feature: str) -> bool:
        if guild_id is None or user_id is None:
            return False
        return feature in self.entries.get(user_key(guild_id, user_id), ())

    def ignored(self, guild_id, user_id) -> bool:
        return self.blocked(guild_id, user_id, "ignore")

    def guild_entries(self, guild_id) -> dict[int, list[str]]:
        prefix = f"{int(guild_id)}:"
        found: dict[int, list[str]] = {}
        for key, features in self.entries.items():
            if not key.startswith(prefix) or not features:
                continue
            try:
                user_id = int(key.split(":", 1)[1])
            except IndexError, ValueError:
                continue
            found[user_id] = [feature for feature in FEATURES if feature in features]
        return dict(sorted(found.items()))

    def set_flag(self, guild_id, user_id, feature: str, enabled: bool) -> bool:
        if feature not in FEATURES:
            raise ValueError(f"未知の制限です: {feature}")
        key = user_key(guild_id, user_id)
        current = set(self.entries.get(key, ()))
        if enabled:
            if feature in current:
                return False
            current.add(feature)
        else:
            if feature not in current:
                return False
            current.discard(feature)
        if current:
            self.entries[key] = current
        else:
            self.entries.pop(key, None)
        self.save()
        return True

    def clear(self, guild_id, user_id) -> bool:
        removed = self.entries.pop(user_key(guild_id, user_id), None)
        changed = bool(removed)
        if changed:
            self.save()
        return changed
