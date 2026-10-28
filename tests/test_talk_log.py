"""会話モードの呼び出し記録（誰が・何回・何を）。"""

import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.conversation.talk_log import MAX_TEXT, TalkLog


class TalkLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "talk_log.jsonl"
        self.stats = Path(self.tmp.name) / "talk_stats.json"
        self.talk = TalkLog(self.log, self.stats)

    def test_counts_per_user_and_channel(self):
        self.assertEqual(self.talk.record(1, 10, 2, "A", "これ要約して"), 1)
        self.talk.record(1, 10, 2, "A", "もう一度")
        self.talk.record(1, 10, 3, "B", "私も", kind="suffix")
        self.talk.record(1, 99, 2, "A", "別チャンネル")

        entry = self.talk.channel_stats(1, 10)
        self.assertEqual(entry["total"], 3)
        self.assertEqual(entry["users"]["2"]["count"], 2)
        self.assertEqual(entry["users"]["3"]["count"], 1)
        self.assertEqual(self.talk.channel_stats(1, 99)["total"], 1)

    def test_ignored_calls_are_recorded_but_not_counted(self):
        self.talk.record(1, 10, 2, "A", "最初の呼び出し")
        self.talk.record(1, 10, 3, "B", "横から", kind="ignored-mention")
        self.talk.record(1, 10, 3, "B", "横から返信", kind="ignored-reply")

        entry = self.talk.channel_stats(1, 10)
        self.assertEqual(entry["total"], 1)
        self.assertEqual(entry["ignored"], 2)
        self.assertNotIn("3", entry["users"])

        self.talk.flush()
        kinds = [
            json.loads(line)["kind"] for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(kinds, ["mention", "ignored-mention", "ignored-reply"])

    def test_detail_records_who_and_what(self):
        self.talk.record(1, 10, 2, "A", "一行目\n二行目を要約して", kind="suffix")
        self.talk.flush()
        record = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["user"], "2")
        self.assertEqual(record["name"], "A")
        self.assertEqual(record["kind"], "suffix")
        self.assertEqual(record["text"], "一行目 二行目を要約して")
        self.assertTrue(record["at"])

    def test_detail_is_written_without_waiting_for_a_full_buffer(self):
        """1件目から明細が残る（まとめ書きだと再起動で消える）。"""
        self.talk.record(1, 10, 2, "A", "すぐ書いて")
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "すぐ書いて")

    def test_long_text_is_clipped(self):
        self.talk.record(1, 10, 2, "A", "あ" * (MAX_TEXT * 3))
        self.talk.flush()
        record = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(record["text"]), MAX_TEXT)
        self.assertTrue(record["text"].endswith("…"))

    def test_counts_survive_restart(self):
        self.talk.record(1, 10, 2, "A", "これ要約して")
        reloaded = TalkLog(self.log, self.stats)
        self.assertEqual(reloaded.channel_stats(1, 10)["total"], 1)
        self.assertEqual(reloaded.channel_stats(1, 10)["users"]["2"]["count"], 1)

    def test_summary_for_panel(self):
        self.assertEqual(self.talk.summary(1, 10), "")
        self.talk.record(1, 10, 2, "A", "ひとつ")
        self.talk.record(1, 10, 2, "A", "ふたつ")
        self.talk.record(1, 10, 3, "B", "みっつ")
        self.talk.record(1, 10, 4, "C", "横槍", kind="ignored-mention")
        text = self.talk.summary(1, 10)
        self.assertIn("呼び出し 3件", text)
        self.assertIn("A 2件", text)
        self.assertIn("横槍 1件は無視", text)

    def test_broken_files_are_ignored(self):
        self.stats.write_text("{壊れた", encoding="utf-8")
        self.assertEqual(TalkLog(self.log, self.stats).channel_stats(1, 10)["total"], 0)

    def test_without_paths_it_keeps_everything_in_memory(self):
        memory = TalkLog(None, None)
        self.assertEqual(memory.record(1, 10, 2, "A", "テスト"), 1)
        memory.flush()
        self.assertEqual(memory.channel_stats(1, 10)["total"], 1)


if __name__ == "__main__":
    unittest.main()
