import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.bookmarks import BookmarkStore, parse_emoji_spec
from jllm_plugins.reader.core import ReaderConfig
from jllm_plugins.reader.panels import ForceVoiceView

from tests.test_reader_core import ReaderCoreTests


class ForceVoiceTests(ReaderCoreTests):
    async def test_forcevoice_set_and_release(self):
        self.cog.config = ReaderConfig(owner_id=99)
        view = ForceVoiceView(self.cog, guild_id=1, owner_id=99, speakers=[(2, "clone")])
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )
        view.pending_user = 42
        view.pending_voice = "2"
        await view.on_apply(interaction)
        self.assertEqual(self.settings.forced_voices.get("1:42"), "clone")
        self.assertTrue(view.finished)

        other = ForceVoiceView(self.cog, guild_id=1, owner_id=99, speakers=[(2, "clone")])
        other.pending_user = 42
        other.pending_voice = "release"
        await other.on_apply(interaction)
        self.assertIsNone(self.settings.forced_voices.get("1:42"))


class BookmarkTests(ReaderCoreTests):
    def setUp(self):
        super().setUp()
        self.cog.config.owner_id = 99
        self.cog.config.bookmark_reaction = "🔖"
        self.cog.bookmarks = BookmarkStore(self.root / "bookmarks.json")
        self.cog.bookmark_rules = parse_emoji_spec("🔖")

    def payload(self):
        return SimpleNamespace(
            user_id=99,
            guild_id=1,
            channel_id=5,
            message_id=10,
            emoji="🔖",
            member=SimpleNamespace(display_name="owner", name="owner"),
        )

    async def test_reaction_add_list_and_delete(self):
        source = SimpleNamespace(
            content="保存したい本文",
            attachments=[],
            embeds=[],
            author=SimpleNamespace(display_name="writer", name="writer"),
        )
        channel = SimpleNamespace(
            name="general",
            fetch_message=AsyncMock(return_value=source),
        )
        user = SimpleNamespace(send=AsyncMock())
        self.cog.bot = SimpleNamespace(
            get_channel=lambda _: channel,
            get_user=lambda _: user,
        )
        payload = self.payload()
        self.assertTrue(self.cog._is_bookmark_reaction(payload))
        await self.cog._add_bookmark(payload)
        items = self.cog.bookmarks.items(99)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["excerpt"], "保存したい本文")
        user.send.assert_awaited_once()

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await self.cog.bookmark.callback(self.cog, interaction)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertEqual(interaction.response.send_message.await_args.kwargs["view"].items, items)

        await self.cog.on_raw_message_delete(payload)
        self.assertEqual(self.cog.bookmarks.items(99), [])

    async def test_reaction_rules_reject_other_users_and_guilds(self):
        payload = self.payload()
        payload.user_id = 42
        self.assertFalse(self.cog._is_bookmark_reaction(payload))
        payload.user_id = 99
        payload.guild_id = None
        self.assertFalse(self.cog._is_bookmark_reaction(payload))


if __name__ == "__main__":
    unittest.main()
