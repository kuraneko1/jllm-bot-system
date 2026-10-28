# ruff: noqa: E501
"""これまでの会話をセッションへ取り込む処理（Discord を叩かない部分）。"""
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.conversation.sessions import SessionStore

from tools.import_sessions import import_messages, is_call, normalize

BOT = "99"
GUILD = "1"
CHANNEL = "10"


def human(mid, text, author="2", name="Aさん", reply_to=None):
    return {
        "id": str(mid),
        "author": {"id": author, "username": name, "global_name": name},
        "content": text,
        "message_reference": {"message_id": str(reply_to)} if reply_to else {},
    }


def bot(mid, text, reply_to):
    return {
        "id": str(mid),
        "author": {"id": BOT, "username": "bot", "bot": True},
        "content": text,
        "message_reference": {"message_id": str(reply_to)},
    }


class NormalizeTests(unittest.TestCase):
    def test_reads_names_and_references(self):
        message = normalize(human(1, "こんにちは", reply_to=5))
        self.assertEqual(message["author_name"], "Aさん")
        self.assertEqual(message["reply_to"], "5")
        self.assertFalse(message["is_bot"])

    def test_member_nickname_wins(self):
        raw = human(1, "やあ")
        raw["member"] = {"nick": "表示名"}
        self.assertEqual(normalize(raw)["author_name"], "表示名")

    def test_broken_rows_are_dropped(self):
        self.assertIsNone(normalize({"author": {}}))

    def test_call_detection(self):
        self.assertTrue(is_call(normalize(human(1, f"<@{BOT}> 要約して")), BOT, ""))
        self.assertTrue(is_call(normalize(human(2, "要約して jbot")), BOT, "jbot"))
        self.assertFalse(is_call(normalize(human(3, "要約して")), BOT, "jbot"))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    def run_import(self, messages, **kwargs):
        return import_messages(
            messages, self.store, BOT, GUILD, CHANNEL, suffix="jbot", model="sarashina3-mini",
            **kwargs,
        )

    def conversation(self):
        return [
            human(100, f"<@{BOT}> これ要約して"),
            bot(101, "## 要点\n- ひとつめ", 100),
            bot(102, "（続き）ふたつめ", 100),
            human(110, "短くして", reply_to=101),
            bot(111, "短くしました", 110),
        ]

    def test_rebuilds_a_conversation_with_pages_joined(self):
        result = self.run_import(self.conversation())
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["turns"], 4)  # 呼び出し1 + 回答(2ページ結合) + 続き1 + 回答1

        session = self.store.message("100")["session_id"]
        history = self.store.history(session)
        self.assertEqual([record["text"] for record in history], [
            "これ要約して",
            "## 要点\n- ひとつめ\n\n（続き）ふたつめ",
            "短くして",
            "短くしました",
        ])
        # Botの回答ページも返信先として登録される
        self.assertEqual(self.store.message("102")["session_id"], session)
        self.assertEqual(self.store.message("111")["session_id"], session)

    def test_other_member_reply_becomes_a_fork(self):
        messages = self.conversation() + [
            human(120, "横から質問です", author="3", name="Bさん", reply_to=111),
            bot(121, "フォークの答え", 120),
        ]
        self.run_import(messages)

        root = self.store.message("100")["session_id"]
        fork = self.store.message("120")["session_id"]
        self.assertNotEqual(root, fork)
        self.assertEqual(self.store.session(fork)["parent_session"], root)
        self.assertEqual(self.store.session(fork)["parent_user"], "3")
        self.assertEqual(
            [record["text"] for record in self.store.history(fork)][-3:],
            ["短くしました", "横から質問です", "フォークの答え"],
        )

    def test_running_twice_does_not_duplicate(self):
        self.run_import(self.conversation())
        before = self.store.message("100")["session_id"]
        second = self.run_import(self.conversation())

        self.assertEqual(second["skipped"], 5)
        self.assertEqual(second["sessions"], 0)
        self.assertEqual(self.store.message("100")["session_id"], before)
        self.assertEqual(self.store.session(before)["turns"], 4)
        self.assertEqual(len(self.store.all_sessions()), 1)

    def test_plain_chatter_and_unknown_replies_are_skipped(self):
        result = self.run_import([
            human(1, "ただの雑談"),
            human(2, "知らない人へのリプ", reply_to=999),
        ])
        self.assertEqual(result["sessions"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(self.store.all_sessions(), [])

    def test_dry_run_writes_nothing(self):
        result = self.run_import(self.conversation(), dry_run=True)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(self.store.all_sessions(), [])

    def test_suffix_calls_are_recognized_and_stripped(self):
        self.run_import([
            human(200, "これ要約して jbot"),
            bot(201, "要約です", 200),
        ])
        session = self.store.message("200")["session_id"]
        self.assertEqual(self.store.history(session)[0]["text"], "これ要約して")
        self.assertEqual(self.store.session(session)["title"], "これ要約して")

    def test_backfill_titles_for_sessions_from_before(self):
        # タイトル機能より前に作られたセッション（直接 append したもの）
        session, _ = self.store.resolve(GUILD, CHANNEL, "2", "Aさん", None)
        self.store.append_user(session, "2", "Aさん", "むかしの質問", "900")
        self.store.update_settings(session, model="")

        with self.store._index() as connection:  # noqa: SLF001 - 状態を作るため
            connection.execute("UPDATE sessions SET title = NULL WHERE session_id = ?", (session,))

        self.assertEqual(self.store.backfill_titles(CHANNEL), 1)
        self.assertEqual(self.store.session(session)["title"], "むかしの質問")

    def test_titles_come_from_the_first_request(self):
        self.run_import([])
        messages = self.conversation()
        self.run_import(messages)
        session = self.store.message("100")["session_id"]
        self.assertEqual(self.store.session(session)["title"], "これ要約して")


if __name__ == "__main__":
    unittest.main()
