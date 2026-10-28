# ruff: noqa: E501
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ForceVoiceView, ReaderCog

SPEAKERS = [(1, "none"), (2, "Clone Ryu"), (3, "Clone Second")]


def make_cog(forced=None, user_voices=None):
    cog = ReaderCog.__new__(ReaderCog)
    cog.bot = None
    cog.players = {}
    cog.forced_voices = dict(forced or {})
    cog.user_voices = dict(user_voices or {})
    cog._save_forced_voices = Mock()
    cog._save_user_voices = Mock()
    return cog


def make_view(cog, owner_id=42):
    return ForceVoiceView(
        cog, guild_id=1, owner_id=owner_id, speakers=SPEAKERS
    )


def interaction(uid):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )


class ForceVoiceViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_blocks_non_owner(self):
        cog = make_cog()
        view = make_view(cog)
        caller = interaction(99)
        view.user_select._values = [SimpleNamespace(id=7)]
        await view.on_select_user(caller)
        caller.response.send_message.assert_awaited_once()
        self.assertIsNone(view.pending_user)

    async def test_apply_sets_forced_voice(self):
        cog = make_cog()
        view = make_view(cog)
        owner = interaction(42)
        view.user_select._values = [SimpleNamespace(id=7)]
        await view.on_select_user(owner)
        view.speaker_select._values = ["2"]
        await view.on_select_voice(owner)
        await view.on_apply(owner)
        self.assertEqual(cog.forced_voices, {"1:7": "Clone Ryu"})
        cog._save_forced_voices.assert_called_once()
        self.assertTrue(view.finished)

    async def test_apply_requires_both_selections(self):
        cog = make_cog()
        view = make_view(cog)
        owner = interaction(42)
        view.user_select._values = [SimpleNamespace(id=7)]
        await view.on_select_user(owner)
        await view.on_apply(owner)
        owner.response.send_message.assert_awaited_once()
        self.assertFalse(cog.forced_voices)
        self.assertFalse(view.finished)

    async def test_apply_allows_member_outside_vc(self):
        cog = make_cog()
        view = make_view(cog)
        owner = interaction(42)
        view.user_select._values = [SimpleNamespace(id=9)]
        await view.on_select_user(owner)
        view.speaker_select._values = ["2"]
        await view.on_select_voice(owner)
        await view.on_apply(owner)
        self.assertEqual(cog.forced_voices, {"1:9": "Clone Ryu"})
        self.assertTrue(view.finished)

    async def test_apply_rejects_unknown_speaker(self):
        cog = make_cog()
        view = make_view(cog)
        owner = interaction(42)
        view.user_select._values = [SimpleNamespace(id=7)]
        await view.on_select_user(owner)
        view.speaker_select._values = ["999"]
        await view.on_select_voice(owner)
        await view.on_apply(owner)
        self.assertFalse(cog.forced_voices)
        self.assertFalse(view.finished)

    async def test_release_removes_forced_voice(self):
        cog = make_cog(forced={"1:7": "Clone Ryu"})
        view = make_view(cog)
        owner = interaction(42)
        view.user_select._values = [SimpleNamespace(id=7)]
        await view.on_select_user(owner)
        view.speaker_select._values = ["release"]
        await view.on_select_voice(owner)
        await view.on_apply(owner)
        self.assertEqual(cog.forced_voices, {})
        cog._save_forced_voices.assert_called_once()

    async def test_embed_lists_current_forced_settings(self):
        cog = make_cog(forced={"1:7": "Clone Ryu"})
        view = make_view(cog)
        field = next(f for f in view.embed().fields if f.name == "現在の強制設定")
        self.assertIn("<@7>", field.value)
        self.assertIn("Clone Ryu", field.value)

    async def test_timeout_disables_panel(self):
        cog = make_cog()
        view = make_view(cog)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.finished)
        self.assertTrue(all(child.disabled for child in view.children))


class ForceVoiceCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = make_cog()
        self.guild = SimpleNamespace(
            id=1,
            voice_channels=[SimpleNamespace(members=[SimpleNamespace(id=7, bot=False)])],
            stage_channels=[],
        )

    def command_interaction(self, uid):
        return SimpleNamespace(
            user=SimpleNamespace(id=uid),
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_non_owner_and_unset_owner_denied(self):
        with patch.object(self.cog.config, "owner_id", 42):
            caller = self.command_interaction(99)
            await ReaderCog.forcevoice.callback(self.cog, caller)
            caller.response.send_message.assert_awaited_once()
            caller.response.defer.assert_not_awaited()
        with patch.object(self.cog.config, "owner_id", 0):
            caller = self.command_interaction(42)
            await ReaderCog.forcevoice.callback(self.cog, caller)
            caller.response.send_message.assert_awaited_once()

    async def test_empty_vc_reports_without_panel(self):
        self.guild.voice_channels = []
        with patch.object(self.cog.config, "owner_id", 42):
            caller = self.command_interaction(42)
            await ReaderCog.forcevoice.callback(self.cog, caller)
            caller.response.send_message.assert_awaited_once()
            caller.followup.send.assert_not_awaited()

    async def test_empty_vc_with_recent_poster_opens_panel(self):
        async def history(limit=20):
            yield SimpleNamespace(
                author=SimpleNamespace(id=9, bot=False), webhook_id=None
            )
        self.guild.voice_channels = []
        self.cog._flatten_speakers = AsyncMock(return_value=SPEAKERS)
        with patch.object(self.cog.config, "owner_id", 42):
            caller = self.command_interaction(42)
            caller.channel = SimpleNamespace(history=history)
            await ReaderCog.forcevoice.callback(self.cog, caller)
            caller.response.send_message.assert_not_awaited()
            view = caller.followup.send.call_args.kwargs["view"]
            self.assertIsInstance(view, ForceVoiceView)

    async def test_owner_opens_panel(self):
        self.cog._flatten_speakers = AsyncMock(return_value=SPEAKERS)
        with patch.object(self.cog.config, "owner_id", 42):
            caller = self.command_interaction(42)
            await ReaderCog.forcevoice.callback(self.cog, caller)
            view = caller.followup.send.call_args.kwargs["view"]
            self.assertIsInstance(view, ForceVoiceView)


class ForcedVoicePrecedenceTests(unittest.IsolatedAsyncioTestCase):
    def test_effective_voice_prefers_forced(self):
        cog = make_cog(forced={"1:7": "Clone Ryu"}, user_voices={"1:7": "Clone Second"})
        self.assertEqual(cog.effective_voice_for(1, 7), "Clone Ryu")
        self.assertEqual(cog.effective_voice_for(1, 8), None)
        cog.forced_voices = {}
        cog.user_voices = {"1:7": "Clone Second"}
        self.assertEqual(cog.effective_voice_for(1, 7), "Clone Second")

    async def test_on_message_uses_forced_speaker(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1, enqueue=Mock(),
        )
        cog.players = {1: player}
        cog.dictionary = SimpleNamespace(
            split=lambda value: [SimpleNamespace(kind="text", value=value)]
        )
        cog.readings = SimpleNamespace(apply=lambda value: value)
        cog.user_voices = {"1:2": "Clone Second"}
        cog.forced_voices = {"1:2": "Clone Ryu"}
        cog.voice_volumes = SimpleNamespace(get=Mock(return_value=1.0))
        cog.voice_speeds = SimpleNamespace(get=Mock(return_value=None))
        cog._voice_id_names = {}
        message = SimpleNamespace(
            id=11, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content="hi", clean_content="hi",
        )
        with patch.object(cog.config, "allowed_channel_ids", {123}):
            await cog.on_message(message)
        player.enqueue.assert_called_once_with("hi", "Clone Ryu", 1.2, "11", "1:2", volume=1.0, effects=())

    def test_persistence_round_trip(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "forced_voices.json")
        with patch("jllm_plugins.reader.core.ReaderConfig.forced_voices_path", path):
            cog = ReaderCog.__new__(ReaderCog)
            cog.config.forced_voices_path = path
            self.assertEqual(cog._load_forced_voices(), {})
            cog.forced_voices = {"1:7": "Clone Ryu"}
            cog._save_forced_voices()
            fresh = ReaderCog.__new__(ReaderCog)
            fresh.config.forced_voices_path = path
            self.assertEqual(fresh._load_forced_voices(), {"1:7": "Clone Ryu"})


if __name__ == "__main__":
    unittest.main()
