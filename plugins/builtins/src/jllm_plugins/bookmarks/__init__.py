"""リアクションで付けるブックマーク。

特定の絵文字（既定はサーバー独自の ``bukuma``）を押した投稿を、**押した人ごとに**
保存する。元の投稿本文は丸ごとは持たず、冒頭の抜粋とジャンプ先だけを残すので、
投稿が消えても壊れず、本文の複製を溜め込むこともない。

保存形式::

    {
      "version": 1,
      "users": {
        "<user_id>": {
          "name": "表示名",
          "items": [
            {"message_id": "...", "channel_id": "...", "guild_id": "...",
             "channel": "general", "author": "名前", "excerpt": "本文の冒頭",
             "added_at": "2026-10-12T08:00:00+00:00"}
          ]
        }
      }
    }

``discord`` に依存しないので、そのまま単体テストできる。
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

EXCERPT_LENGTH = 200
LIST_EXCERPT_LENGTH = 60
MAX_ITEMS_PER_USER = 500

_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_]+")


def jump_url(item) -> str:
    """Discord のメッセージへの直リンク。"""
    return "https://discord.com/channels/{}/{}/{}".format(
        item.get("guild_id") or "@me",
        item.get("channel_id") or "",
        item.get("message_id") or "",
    )


def excerpt(text: str, limit: int = EXCERPT_LENGTH) -> str:
    """1行に潰した抜粋。改行や連続空白は詰める。"""
    flat = " ".join(str(text or "").split())

    if not flat:
        return ""

    if len(flat) <= limit:
        return flat

    return flat[: limit - 1] + "…"


def format_added_at(value: str) -> str:
    """保存日時を ``2026-10-12 08:00`` の形にする。"""
    try:
        moment = datetime.fromisoformat(str(value))
    except TypeError, ValueError:
        return ""

    if moment.tzinfo is not None:
        moment = moment.astimezone()

    return moment.strftime("%Y-%m-%d %H:%M")


def parse_emoji_spec(spec: str) -> list[tuple[str, str]]:
    """``BOOKMARK_REACTION`` の指定を (種類, 値) の一覧にする。

    指定できる形（カンマ・空白区切りで複数可）::

        <:bukuma:1559136627320950865>   カスタム絵文字そのまま
        bukuma:1559136627320950865      名前:ID
        1559136627320950865             ID だけ
        :bukuma: / bukuma               名前（ID を控えておく方が名前変更に強い）
        🔖                                Unicode 絵文字
    """
    rules: list[tuple[str, str]] = []

    for raw in re.split(r"[,\s]+", str(spec or "").strip()):
        if not raw:
            continue

        custom = _CUSTOM_EMOJI.fullmatch(raw)
        if custom:
            rules.append(("id", custom.group(2)))
            continue

        parts = raw.strip(":").split(":")

        if len(parts) == 2 and parts[1].isdigit():
            rules.append(("id", parts[1]))
            continue

        token = parts[0]

        if not token:
            continue

        if token.isdigit():
            rules.append(("id", token))
        elif _PLAIN_NAME.fullmatch(token):
            rules.append(("name", token.casefold()))
        else:
            rules.append(("char", token))

    return rules


def emoji_display(spec: str) -> str:
    """設定文字列を、Discord で絵文字として表示できる形にする。"""
    raw = str(spec or "").strip().split(",")[0].strip()

    if not raw:
        return ""

    if _CUSTOM_EMOJI.fullmatch(raw):
        return raw

    parts = raw.strip(":").split(":")

    if len(parts) == 2 and parts[1].isdigit():
        return f"<:{parts[0]}:{parts[1]}>"

    token = parts[0]

    if token.isdigit():
        return ""

    if _PLAIN_NAME.fullmatch(token):
        return f":{token}:"

    return token


def emoji_matches(emoji, rules) -> bool:
    """``discord.PartialEmoji`` などが指定の絵文字かどうか。"""
    if not rules:
        return False

    emoji_id = getattr(emoji, "id", None)
    emoji_id = str(emoji_id) if emoji_id else ""
    emoji_name = getattr(emoji, "name", None) or ""

    for kind, value in rules:
        if kind == "id" and emoji_id and emoji_id == value:
            return True
        if kind == "name" and emoji_name and emoji_name.casefold() == value:
            return True
        if kind == "char" and str(emoji) == value:
            return True

    return False


class BookmarkStore:
    """ユーザーID → ブックマーク一覧。"""

    def __init__(self, path=None, max_items: int = MAX_ITEMS_PER_USER):
        self.path = Path(path) if path else None
        self.max_items = max(1, int(max_items))
        self.users: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ 読み書き

    def _load(self):
        if self.path is None or not self.path.exists():
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return

        if not isinstance(data, dict):
            return

        users = data.get("users")

        if not isinstance(users, dict):
            return

        for user_id, entry in users.items():
            if not isinstance(entry, dict):
                continue

            items = [
                item
                for item in entry.get("items") or []
                if isinstance(item, dict) and item.get("message_id")
            ]

            if not items:
                continue

            self.users[str(user_id)] = {
                "name": str(entry.get("name") or ""),
                "items": items,
            }

    def save(self):
        if self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "users": self.users}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    # -------------------------------------------------------------------- 操作

    def items(self, user_id) -> list[dict]:
        entry = self.users.get(str(user_id))
        return list(entry["items"]) if entry else []

    def count(self, user_id) -> int:
        return len(self.items(user_id))

    def has(self, user_id, message_id) -> bool:
        key = str(message_id)
        return any(str(item.get("message_id")) == key for item in self.items(user_id))

    def add(self, user_id, user_name: str, item: dict) -> int:
        """1件足して、その人の合計件数を返す。同じ投稿は先頭へ移すだけ。"""
        key = str(user_id)
        entry = self.users.setdefault(
            key,
            {"name": str(user_name or ""), "items": []},
        )
        entry["name"] = str(user_name or entry.get("name") or "")

        message_id = str(item.get("message_id") or "")
        entry["items"] = [
            existing for existing in entry["items"] if str(existing.get("message_id")) != message_id
        ]

        record = dict(item)
        record["message_id"] = message_id
        record.setdefault("added_at", datetime.now(UTC).isoformat())
        entry["items"].insert(0, record)
        del entry["items"][self.max_items :]

        self.save()
        return len(entry["items"])

    def remove(self, user_id, message_id) -> bool:
        """その人の1件を消す。消せたら True。"""
        key = str(user_id)
        entry = self.users.get(key)

        if not entry:
            return False

        wanted = str(message_id)
        remaining = [item for item in entry["items"] if str(item.get("message_id")) != wanted]

        if len(remaining) == len(entry["items"]):
            return False

        entry["items"] = remaining

        if not remaining:
            self.users.pop(key, None)

        self.save()
        return True

    def remove_message(self, message_id) -> int:
        """投稿が消えたとき、全ユーザーの該当項目を外す。消した件数を返す。"""
        wanted = str(message_id)
        removed = 0

        for key in list(self.users):
            entry = self.users[key]
            remaining = [item for item in entry["items"] if str(item.get("message_id")) != wanted]
            removed += len(entry["items"]) - len(remaining)

            if not remaining:
                self.users.pop(key, None)
            else:
                entry["items"] = remaining

        if removed:
            self.save()

        return removed
