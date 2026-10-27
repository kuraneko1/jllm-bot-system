import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.bookmarks import (
    BookmarkStore,
    emoji_display,
    emoji_matches,
    excerpt,
    format_added_at,
    jump_url,
    parse_emoji_spec,
)

BUKUMA = "1559136627320950865"


class _Emoji:
    """discord.PartialEmoji の代わり。"""

    def __init__(self, name=None, emoji_id=None, animated=False):
        self.name = name
        self.id = emoji_id
        self.animated = animated

    def __str__(self):
        if self.id:
            return "<{}:{}:{}>".format("a" if self.animated else "", self.name, self.id)
        return self.name or ""


class EmojiSpecTests(unittest.TestCase):
    def test_custom_emoji_by_id(self):
        rules = parse_emoji_spec(f"<:bukuma:{BUKUMA}>")
        self.assertEqual(rules, [("id", BUKUMA)])
        self.assertTrue(emoji_matches(_Emoji("bukuma", BUKUMA), rules))
        self.assertTrue(emoji_matches(_Emoji("renamed", BUKUMA), rules))
        self.assertFalse(emoji_matches(_Emoji("bukuma", "999"), rules))

    def test_name_id_and_bare_id(self):
        for spec in ("bukuma:" + BUKUMA, BUKUMA):
            self.assertEqual(parse_emoji_spec(spec), [("id", BUKUMA)])

    def test_name_and_colon_name(self):
        for spec in ("bukuma", ":bukuma:"):
            rules = parse_emoji_spec(spec)
            self.assertEqual(rules, [("name", "bukuma")])
            self.assertTrue(emoji_matches(_Emoji("BUKUMA", 123), rules))
            self.assertFalse(emoji_matches(_Emoji("other", 123), rules))

    def test_unicode_emoji(self):
        rules = parse_emoji_spec("🔖")
        self.assertEqual(rules, [("char", "🔖")])
        self.assertTrue(emoji_matches(_Emoji("🔖"), rules))
        self.assertFalse(emoji_matches(_Emoji("⭐"), rules))

    def test_multiple_specs(self):
        rules = parse_emoji_spec(f"bukuma:{BUKUMA}, <a:bukuma_gaming:999>")
        self.assertEqual(rules, [("id", BUKUMA), ("id", "999")])
        self.assertTrue(emoji_matches(_Emoji("bukuma_gaming", "999", True), rules))

    def test_empty_spec_matches_nothing(self):
        self.assertEqual(parse_emoji_spec(""), [])
        self.assertFalse(emoji_matches(_Emoji("bukuma", BUKUMA), []))

    def test_display(self):
        self.assertEqual(emoji_display("bukuma:" + BUKUMA), f"<:bukuma:{BUKUMA}>")
        self.assertEqual(
            emoji_display(f"<:bukuma:{BUKUMA}>"), f"<:bukuma:{BUKUMA}>"
        )
        self.assertEqual(emoji_display(":bukuma:"), ":bukuma:")
        self.assertEqual(emoji_display("🔖"), "🔖")
        self.assertEqual(emoji_display(BUKUMA), "")


class ExcerptTests(unittest.TestCase):
    def test_flattens_newlines(self):
        self.assertEqual(excerpt("一行目\n二行目"), "一行目 二行目")

    def test_truncates(self):
        self.assertEqual(len(excerpt("あ" * 500)), 200)
        self.assertTrue(excerpt("あ" * 500).endswith("…"))

    def test_empty(self):
        self.assertEqual(excerpt(None), "")
        self.assertEqual(excerpt("   "), "")


class BookmarkStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bookmarks.json"
        self.store = BookmarkStore(self.path, max_items=3)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _item(message_id, text="こんにちは"):
        return {
            "message_id": str(message_id),
            "channel_id": "22",
            "guild_id": "11",
            "channel": "general",
            "author": "だれか",
            "excerpt": text,
            "added_at": "2026-10-12T08:00:00+00:00",
        }

    def test_add_is_per_user(self):
        self.store.add(1, "one", self._item(100))
        self.store.add(2, "two", self._item(200))
        self.assertEqual(self.store.count(1), 1)
        self.assertEqual(self.store.count(2), 1)
        self.assertTrue(self.store.has(1, 100))
        self.assertFalse(self.store.has(1, 200))

    def test_newest_first_and_no_duplicates(self):
        self.store.add(1, "one", self._item(100))
        self.store.add(1, "one", self._item(300))
        self.store.add(1, "one", self._item(100))
        self.assertEqual([i["message_id"] for i in self.store.items(1)], ["100", "300"])

    def test_caps_items(self):
        for message_id in range(5):
            self.store.add(1, "one", self._item(message_id))
        self.assertEqual(
            [i["message_id"] for i in self.store.items(1)],
            ["4", "3", "2"],
        )

    def test_remove(self):
        self.store.add(1, "one", self._item(100))
        self.assertTrue(self.store.remove(1, 100))
        self.assertFalse(self.store.remove(1, 100))
        self.assertEqual(self.store.items(1), [])

    def test_remove_message_across_users(self):
        self.store.add(1, "one", self._item(100))
        self.store.add(2, "two", self._item(100))
        self.assertEqual(self.store.remove_message(100), 2)
        self.assertEqual(self.store.remove_message(100), 0)

    def test_persists_and_reloads(self):
        self.store.add(1, "one", self._item(100))
        reloaded = BookmarkStore(self.path, max_items=3)
        self.assertEqual(reloaded.items(1), self.store.items(1))

    def test_ignores_broken_file(self):
        self.path.write_text("{壊れた", encoding="utf-8")
        self.assertEqual(BookmarkStore(self.path).items(1), [])

    def test_saved_file_is_readable_json(self):
        self.store.add(1, "one", self._item(100))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertIn("1", data["users"])


class JumpUrlTests(unittest.TestCase):
    def test_jump_url(self):
        self.assertEqual(
            jump_url({"guild_id": "1", "channel_id": "2", "message_id": "3"}),
            "https://discord.com/channels/1/2/3",
        )

    def test_added_at_format(self):
        # タイムゾーン無しはそのまま、壊れた値は空文字。
        self.assertEqual(format_added_at("2026-10-12T08:00:00"), "2026-10-12 08:00")
        self.assertTrue(format_added_at("2026-10-12T08:00:00+00:00").startswith("2026-10-12"))
        self.assertEqual(format_added_at("こわれている"), "")


if __name__ == "__main__":
    unittest.main()
