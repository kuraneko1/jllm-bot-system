"""会話セッション（1セッション=1 SQLite）とフォークの規則。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.conversation.sessions import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    def files(self):
        return sorted(p.name for p in (Path(self.tmp.name) / "sessions").glob("*.sqlite"))

    def test_each_session_is_its_own_sqlite_file(self):
        session, _ = self.store.resolve(1, 10, 2, "Aさん", "100")
        self.store.append_user(session, 2, "Aさん", "こんにちは", "100")
        self.store.append_assistant(session, "やあ", "101")
        self.assertIn(f"{session}.sqlite", self.files())
        self.assertIn("index.sqlite", self.files())

        # セッションファイル単体に会話が入っている
        path = Path(self.tmp.name) / "sessions" / f"{session}.sqlite"
        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT role, content FROM turns ORDER BY id").fetchall()
        self.assertEqual(rows, [("user", "こんにちは"), ("assistant", "やあ")])

    def test_starting_user_becomes_the_parent(self):
        session, action = self.store.resolve(1, 10, 2, "Aさん", None)
        self.assertEqual(action, "new")
        row = self.store.session(session)
        self.assertEqual(row["parent_user"], "2")
        self.assertEqual(row["parent_user_name"], "Aさん")

    def test_reply_by_the_parent_continues(self):
        session, _ = self.store.resolve(1, 10, 2, "Aさん", None)
        self.store.append_user(session, 2, "Aさん", "最初", "100")
        self.store.append_assistant(session, "答え", "101")

        again, action = self.store.resolve(1, 10, 2, "Aさん", "101")
        self.assertEqual((again, action), (session, "continue"))

    def test_reply_by_another_user_forks_at_that_point(self):
        session, _ = self.store.resolve(1, 10, 2, "Aさん", None)
        self.store.append_user(session, 2, "Aさん", "最初", "100")
        self.store.append_assistant(session, "答え", "101")

        fork, action = self.store.resolve(1, 10, 3, "Bさん", "101")
        self.assertEqual(action, "fork")
        self.assertNotEqual(fork, session)

        row = self.store.session(fork)
        self.assertEqual(row["parent_session"], session)
        self.assertEqual(row["parent_user"], "3")
        self.assertEqual(row["parent_user_name"], "Bさん")
        # 分岐点までの会話を引き継いでいる
        self.assertEqual(
            [record["text"] for record in self.store.history(fork)],
            ["最初", "答え"],
        )

    def test_fork_is_reused_for_the_same_user_and_point(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "最初", "100")
        self.store.append_assistant(session, "答え", "101")
        fork, _ = self.store.resolve(1, 10, 3, "B", "101")

        again, action = self.store.resolve(1, 10, 3, "B", "101")
        self.assertEqual((again, action), (fork, "continue"))
        self.assertEqual(len(self.files()), 3)  # index + 2セッション

    def test_fork_grows_independently(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "最初", "100")
        self.store.append_assistant(session, "答え", "101")
        fork, _ = self.store.resolve(1, 10, 3, "B", "101")
        self.store.append_user(fork, 3, "B", "横から質問", "102")
        self.store.append_assistant(fork, "フォークの答え", "103")

        self.assertEqual([r["text"] for r in self.store.history(session)], ["最初", "答え"])
        self.assertEqual(
            [r["text"] for r in self.store.history(fork)],
            ["最初", "答え", "横から質問", "フォークの答え"],
        )

    def test_reply_to_unknown_message_starts_a_new_session(self):
        first, _ = self.store.resolve(1, 10, 2, "A", "100")
        second, action = self.store.resolve(1, 10, 2, "A", "999")
        self.assertEqual(action, "new")
        self.assertNotEqual(first, second)

    def test_history_shape_matches_reply_tree(self):
        session, _ = self.store.resolve(1, 10, 2, "Aさん", None)
        self.store.append_user(session, 2, "Aさん", "質問", "100")
        self.store.append_assistant(session, "回答", "101")
        records = self.store.history(session)
        self.assertEqual(records[0]["author"], "Aさん")
        self.assertEqual(records[1]["author"], "Bot")
        self.assertEqual(records[0]["text"], "質問")
        self.assertEqual(records[1]["id"], "101")

    def test_discord_names_are_remembered(self):
        session, _ = self.store.resolve(1, 10, 2, "Aさん", None)
        self.store.append_user(session, 2, "Aさん", "こんにちは", "100")
        self.assertEqual(self.store.user_name("2"), "Aさん")

        # 名前が変わったら新しい表示名で上書きする
        session2, _ = self.store.resolve(1, 10, 2, "A（改名）", None)
        self.store.append_user(session2, 2, "A（改名）", "またね", "200")
        self.assertEqual(self.store.user_name("2"), "A（改名）")

    def test_settings_are_kept_per_session_and_inherited_by_forks(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.update_settings(session, model="sarashina3-mini", system_prompt="人格1")
        self.store.append_user(session, 2, "A", "最初", "100")
        self.store.append_assistant(session, "答え", "101")

        fork, _ = self.store.resolve(1, 10, 3, "B", "101")
        self.assertEqual(self.store.settings(fork)["model"], "sarashina3-mini")
        self.assertEqual(self.store.settings(fork)["system_prompt"], "人格1")

        # 途中で既定を変えても、進行中のセッションは変わらない
        self.store.update_settings(session, system_prompt="人格2")
        self.assertEqual(self.store.settings(session)["system_prompt"], "人格2")
        self.assertEqual(self.store.settings(fork)["system_prompt"], "人格1")

    def test_sessions_for_channel(self):
        first, _ = self.store.resolve(1, 10, 2, "A", None)
        second, _ = self.store.resolve(1, 10, 3, "B", None)
        self.store.resolve(1, 99, 2, "A", None)
        ids = [row["session_id"] for row in self.store.sessions_for_channel(1, 10)]
        self.assertCountEqual(ids, [first, second])

    def test_dump_for_inspection(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "こんにちは", "100")
        data = self.store.dump(session)
        self.assertEqual(data["session"]["session_id"], session)
        self.assertEqual(data["turns"][0]["content"], "こんにちは")

    def test_session_ids_do_not_collide_when_created_in_a_burst(self):
        # 採番がぶつかっても採り直す（Bot と取り込みツールの同時実行対策）
        first, _ = self.store.resolve(1, 10, 2, "A", None)
        original = self.store._insert_session  # noqa: SLF001 - 割り込みを再現するため
        calls = {"count": 0}

        def flaky(session_id, *args, **kwargs):
            calls["count"] += 1

            if calls["count"] == 1:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: sessions.session_id")

            return original(session_id, *args, **kwargs)

        self.store._insert_session = flaky  # noqa: SLF001
        self.store._next_session_id = lambda channel_id: "10-0002"  # noqa: SLF001
        second, action = self.store.resolve(1, 10, 3, "B", None)
        self.assertEqual(action, "new")
        self.assertEqual(second, "10-0002")
        self.assertGreaterEqual(calls["count"], 2)
        self.assertNotEqual(second, first)

    def test_discord_timestamps_are_kept(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(
            session, 2, "A", "むかしの質問", "100", created_at="2026-08-01T01:02:03+00:00"
        )
        self.store.append_assistant(
            session, "むかしの答え", "101", created_at="2026-08-01T01:02:10+00:00"
        )

        turns = self.store.detail(session)["turns"]
        self.assertEqual(turns[0]["created_at"], "2026-08-01T01:02:03+00:00")
        self.assertEqual(turns[1]["created_at"], "2026-08-01T01:02:10+00:00")

        # セッションの作成・更新も発言の時刻に合わせる（取り込み時に「今」にならない）
        row = self.store.session(session)
        self.assertEqual(row["created_at"], "2026-08-01T01:02:03+00:00")
        self.assertEqual(row["updated_at"], "2026-08-01T01:02:10+00:00")

    def test_repair_timestamps_uses_the_message_id(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        # Discord のメッセージIDの上位ビットが投稿時刻（ミリ秒）になっている
        self.store.append_user(session, 2, "A", "こんにちは", "1559571226622566441")
        self.store.append_assistant(session, "やあ", "1559571250000000000")

        fixed = self.store.repair_timestamps()
        self.assertEqual(fixed["turns"], 2)
        turns = self.store.detail(session)["turns"]
        self.assertEqual(turns[0]["created_at"], "2026-10-13T14:19:15.859000+00:00")
        self.assertEqual(turns[1]["created_at"], "2026-10-13T14:19:21.432000+00:00")
        self.assertEqual(self.store.session(session)["updated_at"], turns[1]["created_at"])

    def test_avatar_url_and_users_map(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "こんにちは", "100", avatar="abc123")

        self.assertEqual(
            self.store.avatar_url("2"),
            "https://cdn.discordapp.com/avatars/2/abc123.png?size=64",
        )
        # アイコン未設定なら Discord と同じ既定アイコン
        self.assertRegex(
            self.store.avatar_url("3"),
            r"https://cdn\.discordapp\.com/embed/avatars/[0-5]\.png",
        )
        self.assertEqual(
            self.store.avatar_url("2"),
            self.store.users_map(["2"])["2"]["avatar_url"],
        )

    def test_animated_avatar_is_a_gif(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "やあ", "100", avatar="a_xyz")
        self.assertTrue(self.store.avatar_url("2").endswith("a_xyz.gif?size=64"))

    def test_recovers_when_the_directory_is_deleted(self):
        session, _ = self.store.resolve(1, 10, 2, "A", None)
        self.store.append_user(session, 2, "A", "こんにちは", "100")

        # 運用中に置き場を消しても、次の操作で作り直して続けられる
        for path in sorted((Path(self.tmp.name) / "sessions").iterdir()):
            path.unlink()
        (Path(self.tmp.name) / "sessions").rmdir()

        again, action = self.store.resolve(1, 10, 2, "A", "100")
        self.assertEqual(action, "new")
        self.store.append_user(again, 2, "A", "もう一度", "200")
        self.assertEqual(self.store.dump(again)["turns"][0]["content"], "もう一度")

    def test_append_to_missing_session_raises(self):
        with self.assertRaises(KeyError):
            self.store.append_user("10-9999", 2, "A", "こんにちは", "100")


if __name__ == "__main__":
    unittest.main()
