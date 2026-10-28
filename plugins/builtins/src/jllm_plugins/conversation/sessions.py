"""会話セッション（1セッション = 1 SQLite ファイル）。

配置::

    data/sessions/index.sqlite        … ルーティング（メッセージ→セッション）・ユーザー名・一覧
    data/sessions/<session_id>.sqlite … そのセッションの会話（turns）と設定（meta）

考え方
------
- 会話を始めた人（メンション／語尾 `jbot`）を **parent_user** とし、そのセッションを作る。
- Bot（またはそのセッション内の投稿）への**返信**が会話の継続になる。
  続けられるのは parent_user だけで、**別の人が返信したら、その返信時点までの
  会話を引き継いだフォーク**を新しいセッションとして作る（相手はそのフォークの親）。
  同じ人が同じ地点から二度フォークしても、既にあるフォークを再利用する。
- ユーザー名は Discord の表示名。セッション側にも投稿時点の名前を残す。

モデルとシステムプロンプトは**セッション作成時に決めて保存**する。会話の途中で
設定を変えても、進行中のセッションの人格は変わらない（新しいセッションから変わる）。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

INDEX_NAME = "index.sqlite"
HISTORY_TURNS = 120

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    guild_id TEXT, channel_id TEXT,
    parent_user TEXT, parent_user_name TEXT,
    root_message_id TEXT,
    parent_session TEXT, forked_at_turn INTEGER,
    created_at TEXT, updated_at TEXT,
    turns INTEGER DEFAULT 0,
    file TEXT,
    model TEXT, system_prompt TEXT,
    title TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT, turn_id INTEGER,
    channel_id TEXT, author_id TEXT, is_bot INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY, name TEXT, avatar TEXT,
    turns INTEGER DEFAULT 0,
    first_seen TEXT, updated_at TEXT
);
"""

_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    user_id TEXT, user_name TEXT,
    content TEXT,
    message_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(value) -> str:
    """Discord の時刻（datetime / ISO文字列）を保存用のISOへ。"""
    if value is None or value == "":
        return _now()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)

        return value.astimezone(UTC).isoformat()

    return str(value)


def snowflake_time(message_id) -> str:
    """Discord のメッセージIDから投稿時刻を復元する（未取得の分の補修用）。"""
    moment = (int(message_id) >> 22) + 1420070400000
    return datetime.fromtimestamp(moment / 1000, UTC).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    """1操作ごとに開いて閉じる（スレッドをまたがない）。

    置き場（data/sessions/）が消えていても、その場で作り直して開く。
    運用中にディレクトリを消しても復旧できるようにするため。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=3000")
    return connection


class SessionStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index_path = self.directory / INDEX_NAME

        # 索引を作っておく（以降の各操作でも毎回保証される）
        with self._index():
            pass

    @staticmethod
    def _migrate(connection):
        """古い index.sqlite に足りない列を補う。"""
        present = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}

        for column in ("title",):
            if column not in present:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")

        users = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}

        for column in ("avatar",):
            if column not in users:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")

    @contextmanager
    def _index(self):
        """索引DB。ファイルやテーブルが消えていてもその場で作り直す。

        正常終了時に commit し、必ず閉じる。
        """
        connection = _connect(self.index_path)
        connection.executescript(_INDEX_SCHEMA)
        self._migrate(connection)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _session_db(self, session_id):
        connection = _connect(self._file(session_id))
        connection.executescript(_SESSION_SCHEMA)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    # ------------------------------------------------------------------ 参照

    def session(self, session_id) -> dict | None:
        with self._index() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()

        return dict(row) if row else None

    def message(self, message_id) -> dict | None:
        if message_id in (None, ""):
            return None

        with self._index() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (str(message_id),)
            ).fetchone()

        return dict(row) if row else None

    def user_name(self, user_id) -> str:
        with self._index() as connection:
            row = connection.execute(
                "SELECT name FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()

        return row["name"] if row else ""

    def sessions_for_channel(self, guild_id, channel_id, limit=10) -> list[dict]:
        with self._index() as connection:
            rows = connection.execute(
                "SELECT session_id, parent_user, parent_user_name, turns, created_at,"
                " updated_at, parent_session, forked_at_turn, model, title"
                " FROM sessions WHERE guild_id = ? AND channel_id = ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (str(guild_id), str(channel_id), int(limit)),
            ).fetchall()

        return [dict(row) for row in rows]

    def active_partners(self, channel_id, within_seconds=None) -> list[int]:
        """無操作が within_seconds 以内のセッションの親ユーザー（会話中の相手）。

        ``None`` なら時間で切らず、そのチャンネルの全セッションを返す。
        """
        with self._index() as connection:
            rows = connection.execute(
                "SELECT parent_user, updated_at FROM sessions WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchall()

        if within_seconds is None:
            return sorted({int(row["parent_user"]) for row in rows if row["parent_user"]})

        cutoff = datetime.now(UTC).timestamp() - float(within_seconds)
        partners = set()

        for row in rows:
            try:
                seen = datetime.fromisoformat(row["updated_at"]).timestamp()
            except TypeError, ValueError:
                continue

            if seen >= cutoff and row["parent_user"]:
                partners.add(int(row["parent_user"]))

        return sorted(partners)

    def all_sessions(self, limit=200, channel_id=None, guild_id=None) -> list[dict]:
        """新しい順の一覧（Web UI 用）。"""
        query = (
            "SELECT session_id, guild_id, channel_id, parent_user, parent_user_name,"
            " turns, created_at, updated_at, parent_session, forked_at_turn, model, title"
            " FROM sessions"
        )
        where, params = [], []

        if guild_id:
            where.append("guild_id = ?")
            params.append(str(guild_id))

        if channel_id:
            where.append("channel_id = ?")
            params.append(str(channel_id))

        if where:
            query += " WHERE " + " AND ".join(where)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))

        with self._index() as connection:
            rows = connection.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def count_sessions(self, channel_id=None, guild_id=None) -> int:
        """絞り込み条件での総数（一覧は上限つきなので、UI で「200/509」を出す）。"""
        query = "SELECT COUNT(*) AS n FROM sessions"
        where, params = [], []

        if guild_id:
            where.append("guild_id = ?")
            params.append(str(guild_id))

        if channel_id:
            where.append("channel_id = ?")
            params.append(str(channel_id))

        if where:
            query += " WHERE " + " AND ".join(where)

        with self._index() as connection:
            return int(connection.execute(query, params).fetchone()["n"])

    def detail(self, session_id, limit=100, clip=2000) -> dict:
        """1セッションの中身（Web UI 用。長い本文は切る）。"""
        session = self.session(session_id)

        if session is None:
            return {}

        path = self._file(session_id)
        rows = []

        if path.exists():
            with self._session_db(session_id) as connection:
                rows = connection.execute(
                    "SELECT id, role, user_id, user_name, content, message_id, created_at"
                    " FROM turns ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()

        turns = []

        for row in reversed(rows):
            text = row["content"] or ""
            truncated = len(text) > clip
            turns.append(
                {
                    "id": row["id"],
                    "role": row["role"],
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "content": text[:clip] + ("…" if truncated else ""),
                    "truncated": truncated,
                    "created_at": row["created_at"],
                }
            )

        return {"session": session, "turns": turns}

    def repair_timestamps(self, channel_id=None) -> dict:
        """メッセージIDから投稿時刻を復元して、取り込み時に付いた時刻を直す。"""
        query = "SELECT session_id FROM sessions"
        params = []

        if channel_id:
            query += " WHERE channel_id = ?"
            params.append(str(channel_id))

        with self._index() as connection:
            ids = [row["session_id"] for row in connection.execute(query, params)]

        fixed = {"turns": 0, "sessions": 0}

        for session_id in ids:
            if not self._file(session_id).exists():
                continue

            with self._session_db(session_id) as connection:
                rows = connection.execute("SELECT id, message_id, created_at FROM turns").fetchall()

                for row in rows:
                    if not row["message_id"]:
                        continue

                    try:
                        moment = snowflake_time(row["message_id"])
                    except TypeError, ValueError:
                        continue

                    if moment != row["created_at"]:
                        connection.execute(
                            "UPDATE turns SET created_at = ? WHERE id = ?",
                            (moment, row["id"]),
                        )
                        fixed["turns"] += 1

                span = connection.execute(
                    "SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM turns"
                ).fetchone()

            if span and span["first"]:
                with self._index() as connection:
                    connection.execute(
                        "UPDATE sessions SET created_at = ?, updated_at = ? WHERE session_id = ?",
                        (span["first"], span["last"] or span["first"], str(session_id)),
                    )

                fixed["sessions"] += 1

        return fixed

    def backfill_titles(self, channel_id=None) -> int:
        """名前が空のセッションに、最初のユーザー発言から名前を付ける。"""
        query = "SELECT session_id FROM sessions WHERE title IS NULL OR title = ''"
        params = []

        if channel_id:
            query += " AND channel_id = ?"
            params.append(str(channel_id))

        with self._index() as connection:
            ids = [row["session_id"] for row in connection.execute(query, params)]

        filled = 0

        for session_id in ids:
            if not self._file(session_id).exists():
                continue

            with self._session_db(session_id) as connection:
                row = connection.execute(
                    "SELECT content FROM turns WHERE role = 'user' ORDER BY id LIMIT 1"
                ).fetchone()

            if row and row["content"]:
                self._set_title_if_empty(session_id, row["content"])
                filled += 1

        return filled

    def for_channel_summary(self, guild_id, channel_id, limit=3) -> str:
        """パネル用の一言（新しい順に「相手・回数・モデル」）。"""
        rows = self.sessions_for_channel(guild_id, channel_id, limit=limit)

        if not rows:
            return ""

        parts = [
            "{} {}往復{}".format(
                row["parent_user_name"] or "<@{}>".format(row["parent_user"]),
                row["turns"],
                "（フォーク）" if row["parent_session"] else "",
            )
            for row in rows
        ]
        return "セッション {}件｜{}".format(len(rows), " / ".join(parts))

    def history(self, session_id, limit=HISTORY_TURNS) -> list[dict]:
        """モデルへ渡す会話ログ（reply_tree と同じ形）。古い順。"""
        path = self._file(session_id)

        if not path.exists():
            return []

        with self._session_db(session_id) as connection:
            rows = connection.execute(
                "SELECT * FROM turns ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()

        records = []

        for row in reversed(rows):
            text = (row["content"] or "").strip()

            if not text:
                continue

            records.append(
                {
                    "id": str(row["message_id"] or "turn-{}".format(row["id"])),
                    "reply_to": None,
                    "author": "Bot" if row["role"] == "assistant" else (row["user_name"] or "不明"),
                    "text": text,
                }
            )

        return records

    # ------------------------------------------------------------------ 解決

    def resolve(self, guild_id, channel_id, user_id, user_name, reply_to_message_id):
        """返信先から「新規・継続・フォーク」を決めてセッションIDを返す。

        戻り値: ``(session_id, action)`` で action は
        ``"new"`` / ``"continue"`` / ``"fork"``。
        """
        user_id = str(user_id)
        target = self.message(reply_to_message_id)

        if target is None:
            return self._create_root(
                guild_id,
                channel_id,
                user_id,
                user_name,
                reply_to_message_id,
            ), "new"

        parent = self.session(target["session_id"])

        if parent is None:
            return self._create_root(
                guild_id,
                channel_id,
                user_id,
                user_name,
                reply_to_message_id,
            ), "new"

        if parent["parent_user"] == user_id:
            return parent["session_id"], "continue"

        existing = self._find_fork(parent["session_id"], target["turn_id"], user_id)

        if existing:
            return existing, "continue"

        return self._create_fork(parent, target, user_id, user_name), "fork"

    def _find_fork(self, parent_session, turn_id, user_id):
        with self._index() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions WHERE parent_session = ?"
                " AND forked_at_turn = ? AND parent_user = ? LIMIT 1",
                (str(parent_session), int(turn_id or 0), str(user_id)),
            ).fetchone()

        return row["session_id"] if row else None

    # ------------------------------------------------------------------ 作成

    def _next_session_id(self, channel_id) -> str:
        prefix = f"{channel_id}"

        with self._index() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id LIKE ?"
                " ORDER BY session_id DESC LIMIT 1",
                (prefix + "-%",),
            ).fetchone()

        number = 1

        if row:
            try:
                number = int(str(row["session_id"]).rsplit("-", 1)[1]) + 1
            except IndexError, ValueError:
                number = 1

        return f"{prefix}-{number:04d}"

    def _create_root(self, guild_id, channel_id, user_id, user_name, root_message_id) -> str:
        return self._create_with_retry(
            channel_id,
            lambda session_id: self._insert_session(
                session_id,
                guild_id,
                channel_id,
                user_id,
                user_name,
                root_message_id,
                None,
                None,
            ),
        )

    def _create_fork(self, parent: dict, target: dict, user_id, user_name) -> str:
        # 同時に別の会話が始まってもIDがぶつからないよう、採番と作成を再試行する。
        session_id = self._create_with_retry(
            parent["channel_id"],
            lambda candidate: self._insert_session(
                candidate,
                parent["guild_id"],
                parent["channel_id"],
                user_id,
                user_name,
                parent["root_message_id"],
                parent["session_id"],
                target["turn_id"],
                model=parent.get("model"),
                system_prompt=parent.get("system_prompt"),
            ),
        )

        # 分岐点までの会話を引き継ぐ。以降は独立して伸びる。
        parent_path = self._file(parent["session_id"])
        fork_turn = int(target["turn_id"] or 0)

        if parent_path.exists():
            with self._session_db(parent["session_id"]) as source:
                rows = source.execute(
                    "SELECT role, user_id, user_name, content, message_id, created_at"
                    " FROM turns WHERE id <= ? ORDER BY id",
                    (fork_turn,),
                ).fetchall()

            with self._session_db(session_id) as child:
                child.executemany(
                    "INSERT INTO turns (role, user_id, user_name, content, message_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [tuple(row) for row in rows],
                )
                child.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('forked_from', ?)",
                    ("{}#{}".format(parent["session_id"], fork_turn),),
                )

            self._set_turns(session_id, len(rows))

        return session_id

    def _create_with_retry(self, channel_id, insert, attempts=8) -> str:
        """セッションIDの採番は他プロセスと競合しうるので、衝突したら採り直す。"""
        for _ in range(attempts):
            session_id = self._next_session_id(channel_id)

            try:
                insert(session_id)
            except sqlite3.IntegrityError:
                continue

            return session_id

        raise RuntimeError("セッションIDを確保できませんでした")

    def _insert_session(
        self,
        session_id,
        guild_id,
        channel_id,
        user_id,
        user_name,
        root_message_id,
        parent_session,
        forked_at_turn,
        model=None,
        system_prompt=None,
    ):
        moment = _now()
        path = self._file(session_id)

        with self._session_db(session_id) as connection:
            connection.executescript(_SESSION_SCHEMA)

        with self._index() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, guild_id, channel_id, parent_user,"
                " parent_user_name, root_message_id, parent_session, forked_at_turn,"
                " created_at, updated_at, turns, file, model, system_prompt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    session_id,
                    str(guild_id),
                    str(channel_id),
                    str(user_id),
                    str(user_name or ""),
                    str(root_message_id or ""),
                    parent_session,
                    forked_at_turn,
                    moment,
                    moment,
                    path.name,
                    model,
                    system_prompt,
                ),
            )

        self.remember_user(user_id, user_name)
        return session_id

    # ------------------------------------------------------------------ 記録

    def create_session(
        self, guild_id, channel_id, user_id, user_name, model=None, system_prompt=None
    ) -> str:
        """会話の根を直接作る（Webチャットなど、Discordの投稿が起点でないとき）。"""
        session_id = self._create_with_retry(
            channel_id,
            lambda candidate: self._insert_session(
                candidate,
                guild_id,
                channel_id,
                user_id,
                user_name,
                None,
                None,
                None,
                model=model,
                system_prompt=system_prompt,
            ),
        )
        return session_id

    def delete_session(self, session_id) -> bool:
        """セッションと、その投稿の対応表を消す。"""
        session = self.session(session_id)

        if session is None:
            return False

        with self._index() as connection:
            connection.execute("DELETE FROM messages WHERE session_id = ?", (str(session_id),))
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (str(session_id),))

        try:
            self._file(session_id).unlink(missing_ok=True)
        except OSError:
            pass

        return True

    def remember_user(self, user_id, name, avatar=None):
        """Discord の表示名とアイコンを覚える（変わったら新しい方で上書き）。"""
        if user_id in (None, ""):
            return

        moment = _now()

        with self._index() as connection:
            connection.execute(
                "INSERT INTO users (user_id, name, avatar, turns, first_seen, updated_at)"
                " VALUES (?, ?, ?, 0, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " name = COALESCE(NULLIF(excluded.name, ''), users.name),"
                " avatar = COALESCE(NULLIF(excluded.avatar, ''), users.avatar),"
                " updated_at = excluded.updated_at",
                (str(user_id), str(name or ""), str(avatar or ""), moment, moment),
            )

    def avatar_url(self, user_id) -> str:
        """Discord のアイコンURL。未設定なら既定アイコンを組み立てる。"""
        if user_id in (None, ""):
            return ""

        with self._index() as connection:
            row = connection.execute(
                "SELECT avatar FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()

        avatar = (row["avatar"] if row else "") or ""

        try:
            number = int(user_id)
        except TypeError, ValueError:
            return ""

        if avatar:
            suffix = "gif" if str(avatar).startswith("a_") else "png"
            return f"https://cdn.discordapp.com/avatars/{number}/{avatar}.{suffix}?size=64"

        # 既定アイコン（Discord と同じ割り当て）
        return f"https://cdn.discordapp.com/embed/avatars/{(number >> 22) % 6}.png"

    def users_map(self, user_ids) -> dict:
        """UI 用に {user_id: {name, avatar_url}} を作る。"""
        wanted = {str(value) for value in user_ids if value not in (None, "")}

        if not wanted:
            return {}

        placeholders = ",".join("?" for _ in wanted)
        with self._index() as connection:
            rows = connection.execute(
                f"SELECT user_id, name, avatar FROM users WHERE user_id IN ({placeholders})",
                sorted(wanted),
            ).fetchall()

        found = {row["user_id"]: dict(row) for row in rows}
        result = {}

        for user_id in sorted(wanted):
            row = found.get(user_id, {})
            result[user_id] = {
                "name": row.get("name") or "",
                "avatar_url": self.avatar_url(user_id) if user_id.isdigit() else "",
            }

        return result

    def append_user(
        self, session_id, user_id, user_name, text, message_id=None, created_at=None, avatar=None
    ) -> int:
        if avatar:
            self.remember_user(user_id, user_name, avatar)

        return self._append(
            session_id, "user", user_id, user_name, text, message_id, count_user=True, at=created_at
        )

    def append_assistant(self, session_id, text, message_id=None, created_at=None) -> int:
        return self._append(session_id, "assistant", None, "", text, message_id, at=created_at)

    def _append(
        self, session_id, role, user_id, user_name, content, message_id, count_user=False, at=None
    ) -> int:
        path = self._file(session_id)

        if not path.exists():
            raise KeyError(f"セッションがありません: {session_id}")

        moment = _iso(at)

        with self._session_db(session_id) as connection:
            cursor = connection.execute(
                "INSERT INTO turns (role, user_id, user_name, content, message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    role,
                    str(user_id) if user_id else None,
                    str(user_name or ""),
                    str(content or ""),
                    str(message_id) if message_id else None,
                    moment,
                ),
            )
            turn_id = int(cursor.lastrowid)

        if role == "user":
            self._set_title_if_empty(session_id, content)
            self.remember_user(user_id, user_name)

        self._set_turns(session_id, None, at=moment)
        self.register_message(
            message_id, session_id, turn_id, role == "assistant", user_id, created_at=moment
        )

        if count_user and user_id:
            self.remember_user(user_id, user_name)
            self._bump_user_turns(user_id)

        return turn_id

    def register_message(
        self, message_id, session_id, turn_id, is_bot=False, author_id=None, created_at=None
    ):
        """Discord のメッセージID → セッション。返信の追跡に使う。"""
        if message_id in (None, ""):
            return

        session = self.session(session_id)

        with self._index() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO messages (message_id, session_id, turn_id,"
                " channel_id, author_id, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(message_id),
                    str(session_id),
                    int(turn_id or 0),
                    str(session["channel_id"]) if session else None,
                    str(author_id) if author_id else None,
                    1 if is_bot else 0,
                    _iso(created_at),
                ),
            )

    def _set_title_if_empty(self, session_id, text):
        """一覧に出す会話名（最初のユーザー発言の冒頭）。"""
        title = " ".join(str(text or "").split())[:60]

        if not title:
            return

        with self._index() as connection:
            connection.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ?"
                " AND (title IS NULL OR title = '')",
                (title, str(session_id)),
            )

    def _bump_user_turns(self, user_id):
        with self._index() as connection:
            connection.execute(
                "UPDATE users SET turns = turns + 1, updated_at = ? WHERE user_id = ?",
                (_now(), str(user_id)),
            )

    def _set_turns(self, session_id, turns=None, at=None):
        """index 側の集計（turns）と時刻を更新する。中身はセッション側のファイルにある。"""
        if turns is None:
            path = self._file(session_id)

            if path.exists():
                with self._session_db(session_id) as connection:
                    turns = int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
            else:
                turns = 0

        moment = _iso(at)

        with self._index() as connection:
            connection.execute(
                "UPDATE sessions SET updated_at = ?, turns = ?,"
                " created_at = MIN(created_at, ?) WHERE session_id = ?",
                (moment, int(turns), moment, str(session_id)),
            )

    # ------------------------------------------------------------------ 設定

    def settings(self, session_id) -> dict:
        """セッションのモデルとシステムプロンプト（作成時の値）。"""
        session = self.session(session_id) or {}
        return {
            "model": session.get("model") or "",
            "system_prompt": session.get("system_prompt") or "",
        }

    def update_settings(self, session_id, model=None, system_prompt=None):
        """進行中のセッションの設定を今の既定へ差し替える。"""
        fields, values = [], []

        if model is not None:
            fields.append("model = ?")
            values.append(str(model))

        if system_prompt is not None:
            fields.append("system_prompt = ?")
            values.append(str(system_prompt))

        if not fields:
            return False

        values.append(str(session_id))

        with self._index() as connection:
            connection.execute(
                "UPDATE sessions SET {} WHERE session_id = ?".format(", ".join(fields)),
                values,
            )

        return True

    # ------------------------------------------------------------------ 補助

    def _file(self, session_id) -> Path:
        return self.directory / f"{session_id}.sqlite"

    def dump(self, session_id) -> dict:
        """人が読む用（確認コマンド・テストで使う）。"""
        session = self.session(session_id)

        if session is None:
            return {}

        path = self._file(session_id)
        turns = []

        if path.exists():
            with self._session_db(session_id) as connection:
                turns = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id, role, user_id, user_name, content, message_id, created_at"
                        " FROM turns ORDER BY id"
                    ).fetchall()
                ]

        return {"session": session, "turns": turns}

    def export_json(self, session_id, target: Path):
        target.write_text(
            json.dumps(self.dump(session_id), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
