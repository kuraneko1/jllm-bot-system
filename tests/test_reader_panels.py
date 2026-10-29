import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.reader.core import ReaderConfig
from jllm_plugins.reader.panels import MYTALK_MUTE_VALUE, MyTalkView, RestrictView

from tests.test_reader_core import ReaderCoreTests


class ReaderPanelTests(ReaderCoreTests):
    async def test_mytalk_hidden_voices_default_custom_and_empty(self):
        speakers = [(1, "demo先輩"), (2, "淫夢(登録なし)"), (3, "clone")]
        for hidden, expected in ((None, {"3"}), (frozenset({"clone"}), {"1", "2"}),
                                 (frozenset(), {"1", "2", "3"})):
            if hidden is not None:
                self.cog.config.mytalk_hidden_voices = hidden
            view = MyTalkView(self.cog, guild_id=1, user_id=42, speakers=speakers)
            self.assertEqual(
                {option.value for option in view.speaker_select.options} - {"mute", "default"},
                expected,
            )

    async def test_mytalk_select_apply_and_mute(self):
        self.cog.config = ReaderConfig()
        view = MyTalkView(
            self.cog, guild_id=1, user_id=42, speakers=[(1, "ノーマル"), (2, "clone")]
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )
        view.speaker_select._values = ["2"]
        await view.on_select(interaction)
        self.assertEqual(view.pending, "2")
        await view.on_apply(interaction)
        self.assertEqual(self.settings.user_voices.get("1:42"), "clone")
        self.assertTrue(view.finished)
        interaction.response.edit_message.assert_awaited()

        other = MyTalkView(self.cog, guild_id=1, user_id=42, speakers=[(1, "ノーマル")])
        other.pending = MYTALK_MUTE_VALUE
        await other.on_apply(interaction)
        self.assertIn(42, self.player.skip_users)

    async def test_mytalk_command_sends_ephemeral_panel(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace())),
        )
        await self.cog.mytalk.callback(self.cog, interaction)
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIsInstance(interaction.followup.send.await_args.kwargs["view"], MyTalkView)

    async def test_restrict_select_toggle_and_clear(self):
        self.cog.config = ReaderConfig(owner_id=99)
        view = RestrictView(self.cog, guild_id=1, owner_id=99)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )
        view.user_select._values = [SimpleNamespace(id=42)]
        await view.on_select_user(interaction)
        self.assertEqual(view.selected_user, 42)
        await view._make_toggle("wav")(interaction)
        self.assertTrue(self.restrictions.blocked(1, 42, "wav"))
        await view.on_clear(interaction)
        self.assertFalse(self.restrictions.blocked(1, 42, "wav"))

    async def test_resettalk_removes_only_current_guild(self):
        self.settings.user_voices.set("1:42", "clone")
        self.settings.user_voices.set("2:42", "other")
        member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch("jllm_plugins.reader.core.discord.Member", type(member)):
            await self.cog.resettalk.callback(self.cog, interaction)
        self.assertIsNone(self.settings.user_voices.get("1:42"))
        self.assertEqual(self.settings.user_voices.get("2:42"), "other")
        self.assertEqual(self.player.speaker_id, 1)


if __name__ == "__main__":
    unittest.main()
