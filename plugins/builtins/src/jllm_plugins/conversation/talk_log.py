"""会話モードの呼び出し記録。

「誰が・何回・何を」呼び出したかを残す。会話の文脈そのものは Discord の
返信チェーンから毎回作り直すので、ここに残すのは**呼び出しの履歴**だけ。

- ``talk_log.jsonl``   … 追記式の明細（新しい順に読める。上限を超えたら古い方を捨てる）
- ``talk_stats.json``  … チャンネル・ユーザーごとの回数（パネル表示に使う）

横槍で無視した呼び出しも ``kind="ignored-mention"`` / ``"ignored-reply"`` として
残すが、回数（``total``）には数えない。
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

MAX_TEXT = 500
MAX_ENTRIES = 2000
KEEP_ENTRIES = 1000

ACCEPTED_KINDS = ("mention", "suffix", "reply")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def clip(text: str) -> str:
    """記録用に1行へ潰して長さを切る。"""
    flat = " ".join(str(text or "").split())

    if len(flat) <= MAX_TEXT:
        return flat

    return flat[: MAX_TEXT - 1] + "…"


class TalkLog:
    """チャンネル・ユーザーごとの呼び出し回数と明細。"""

    def __init__(self, log_path=None, stats_path=None):
        self.log_path = Path(log_path) if log_path else None
        self.stats_path = Path(stats_path) if stats_path else None
        # "guild:channel" -> {"total": int, "ignored": int, "users": {uid: {...}}}
        self.channels: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._load()

    # ------------------------------------------------------------------ 読み書き

    def _load(self):
        if self.stats_path is None or not self.stats_path.exists():
            return

        try:
            data = json.loads(self.stats_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return

        if not isinstance(data, dict):
            return

        channels = data.get("channels")

        if not isinstance(channels, dict):
            return

        for key, entry in channels.items():
            if not isinstance(entry, dict):
                continue

            users = {
                str(uid): {
                    "name": str(value.get("name") or ""),
                    "count": int(value.get("count") or 0),
                    "last": str(value.get("last") or ""),
                }
                for uid, value in (entry.get("users") or {}).items()
                if isinstance(value, dict)
            }
            self.channels[str(key)] = {
                "total": int(entry.get("total") or 0),
                "ignored": int(entry.get("ignored") or 0),
                "users": users,
            }

    def save(self):
        if self.stats_path is None:
            return

        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "channels": self.channels}
        temp = self.stats_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.stats_path)

    # -------------------------------------------------------------------- 記録

    @staticmethod
    def key(guild_id, channel_id) -> str:
        return f"{guild_id}:{channel_id}"

    def record(self, guild_id, channel_id, user_id, user_name, text, kind="mention"):
        """1件記録して、そのチャンネルの通算回数を返す。"""
        accepted = kind in ACCEPTED_KINDS
        key = self.key(guild_id, channel_id)
        entry = self.channels.setdefault(key, {"total": 0, "ignored": 0, "users": {}})
        moment = _now()

        if accepted:
            entry["total"] += 1
            user = entry["users"].setdefault(
                str(user_id),
                {"name": str(user_name or ""), "count": 0, "last": ""},
            )
            user["count"] += 1
            user["last"] = moment
            user["name"] = str(user_name or user.get("name") or "")
        else:
            entry["ignored"] += 1

        self._append(
            {
                "at": moment,
                "guild": str(guild_id),
                "channel": str(channel_id),
                "user": str(user_id),
                "name": str(user_name or ""),
                "kind": kind,
                "text": clip(text),
            }
        )
        self.save()
        return entry["total"]

    def _append(self, record: dict):
        if self.log_path is None:
            return

        self._pending.append(record)

        # 呼び出しは多くて毎分数件なので、その都度書く。まとめ書きにすると
        # 再起動でバッファが消え、明細が残らない（統計だけが増える）。
        self.flush()

    def flush(self):
        if self.log_path is None or not self._pending:
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        with self.log_path.open("a", encoding="utf-8") as handle:
            for record in self._pending:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        self._pending = []
        self._rotate()

    def _rotate(self):
        """明細が増えすぎたら古い方を捨てる（回数は stats 側に残る）。"""
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        if len(lines) <= MAX_ENTRIES:
            return

        kept = lines[-KEEP_ENTRIES:]
        temp = self.log_path.with_suffix(".tmp")
        temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(temp, self.log_path)

    # -------------------------------------------------------------------- 集計

    def channel_stats(self, guild_id, channel_id) -> dict:
        return self.channels.get(
            self.key(guild_id, channel_id), {"total": 0, "ignored": 0, "users": {}}
        )

    def summary(self, guild_id, channel_id, limit=3) -> str:
        """パネル用の1行。呼び出しが無ければ空文字。"""
        entry = self.channel_stats(guild_id, channel_id)

        if not entry["total"] and not entry["ignored"]:
            return ""

        ranked = sorted(
            entry["users"].items(),
            key=lambda item: (-item[1]["count"], item[0]),
        )
        top = " / ".join(
            "{} {}件".format(user["name"] or f"<@{uid}>", user["count"])
            for uid, user in ranked[:limit]
        )
        text = "呼び出し {}件".format(entry["total"])

        if top:
            text += f"（{top}）"

        if entry["ignored"]:
            text += "・横槍 {}件は無視".format(entry["ignored"])

        return text
