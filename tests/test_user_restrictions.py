# ruff: noqa: E501
"""ユーザーごとの制限（wav/GIFの再生剥奪・まるごと無視）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.dictionaries.dictionary import Segment
from jllm_plugins.reader.core import ReaderCog, RestrictView
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class UserRestrictionsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "user_restrictions.json"
        self.store = UserRestrictions(self.path)

    def test_default_is_no_restriction(self):
        self.assertEqual(self.store.flags(1, 2), frozenset())
        self.assertFalse(self.store.blocked(1, 2, "wav"))
        self.assertFalse(self.store.ignored(1, 2))
        self.assertEqual(self.store.guild_entries(1), {})

    def test_set_and_clear(self):
        self.assertTrue(self.store.set_flag(1, 2, "wav", True))
        self.assertFalse(self.store.set_flag(1, 2, "wav", True))  # 変化なし
        self.assertTrue(self.store.blocked(1, 2, "wav"))
        self.assertFalse(self.store.blocked(1, 2, "gif"))

        self.assertTrue(self.store.set_flag(1, 2, "ignore", True))
        self.assertEqual(self.store.guild_entries(1), {2: ["wav", "ignore"]})

        self.assertTrue(self.store.clear(1, 2))
        self.assertFalse(self.store.clear(1, 2))
        self.assertEqual(self.store.flags(1, 2), frozenset())

    def test_users_and_guilds_are_separate(self):
        self.store.set_flag(1, 2, "wav", True)
        self.store.set_flag(1, 3, "gif", True)
        self.store.set_flag(9, 2, "ignore", True)

        self.assertEqual(self.store.guild_entries(1), {2: ["wav"], 3: ["gif"]})
        self.assertFalse(self.store.blocked(1, 2, "ignore"))
        self.assertTrue(self.store.blocked(9, 2, "ignore"))

    def test_survives_reload(self):
        self.store.set_flag(1, 2, "wav", True)
        self.store.set_flag(1, 2, "ignore", True)

        again = UserRestrictions(self.path)

        self.assertEqual(again.flags(1, 2), frozenset({"wav", "ignore"}))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"1:2": ["wav", "ignore"]})

    def test_broken_file_is_ignored(self):
        self.path.write_text("{壊れている", encoding="utf-8")

        store = UserRestrictions(self.path)

        self.assertEqual(store.guild_entries(1), {})

    def test_unknown_feature_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_flag(1, 2, "nope", True)

    def test_unknown_values_are_dropped_on_load(self):
        self.path.write_text(json.dumps({"1:2": ["wav", "nope"], "bad": "x"}), encoding="utf-8")

        store = UserRestrictions(self.path)

        self.assertEqual(store.flags(1, 2), frozenset({"wav"}))
        self.assertEqual(store.guild_entries(1), {2: ["wav"]})

    def test_no_path_is_a_no_op(self):
        store = UserRestrictions(None)

        self.assertTrue(store.set_flag(1, 2, "wav", True))
        self.assertTrue(store.blocked(1, 2, "wav"))
        store.save()  # 落ちない


class ReaderRestrictionTests(unittest.IsolatedAsyncioTestCase):
    """/restrict の効き方（読み上げ側）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = UserRestrictions(self.root / "restrictions.json")
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.bot = SimpleNamespace(restrictions=self.store)
        self.cog._handle_html_registration = AsyncMock(return_value=False)
        self.cog._handle_media_registration = AsyncMock(return_value=False)
        self.cog._send_media = AsyncMock()
        self.cog._react_v100 = AsyncMock()
        self.cog._react_skip = AsyncMock()
        self.player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1, enqueue=Mock(), enqueue_sound=Mock(),
            skip=Mock(return_value=True), skip_current_post=Mock(return_value=True),
        )
        self.cog.players = {1: self.player}
        self.cog.dictionary = SimpleNamespace(
            split=lambda value: [Segment("sound", "/tmp/x.wav", "v100")],
            volume_of_file=lambda name: 1.0,
        )
        self.cog.readings = SimpleNamespace(apply=lambda value: value)
        self.cog.user_voices = {}
        self.cog.forced_voices = {}
        self.cog.voice_volumes = VoiceVolumes()
        self.cog.voice_speeds = VoiceSpeeds()
        self.cog._voice_id_names = {}
        self.cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        self.cog.toggles = {"wav": True, "img": True}

    def message(self, text, *, user=2, mid=1):
        return SimpleNamespace(
            id=mid, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=user, bot=False),
            channel=SimpleNamespace(id=123), content=text, clean_content=text,
            mentions=[], reference=None,
        )

    async def send(self, text, *, user=2, mid=1):
        with patch.object(self.cog.config, "allowed_channel_ids", {123}):
            await self.cog.on_message(self.message(text, user=user, mid=mid))

    async def test_ignored_user_is_not_read_at_all(self):
        self.store.set_flag(1, 2, "ignore", True)

        await self.send("こんにちは")

        self.player.enqueue.assert_not_called()
        self.player.enqueue_sound.assert_not_called()
        self.cog._send_media.assert_not_awaited()

    async def test_wav_is_read_by_tts_when_revoked(self):
        self.store.set_flag(1, 2, "wav", True)

        await self.send("v100")

        # wav は鳴らさず、語句をそのまま読み上げる
        self.player.enqueue_sound.assert_not_called()
        self.player.enqueue.assert_called_once()
        self.assertEqual(self.player.enqueue.call_args.args[0], "v100")

    async def test_wav_plays_for_everyone_else(self):
        await self.send("v100")

        self.player.enqueue_sound.assert_called_once()
        self.player.enqueue.assert_not_called()

    async def test_other_users_are_not_affected(self):
        self.store.set_flag(1, 2, "wav", True)

        await self.send("v100", user=3, mid=2)

        self.player.enqueue_sound.assert_called_once()
        self.player.enqueue.assert_not_called()

    async def test_gif_is_not_sent_when_revoked(self):
        self.cog.media = {"v100": "x.gif"}
        self.cog.media_dir = self.root
        (self.root / "x.gif").write_bytes(b"gif")
        self.cog._send_media = ReaderCog._send_media.__get__(self.cog)
        message = self.message("v100")
        message.channel = SimpleNamespace(id=123, send=AsyncMock())
        self.store.set_flag(1, 2, "gif", True)

        await self.cog._send_media(message)

        message.channel.send.assert_not_awaited()

    async def test_gif_is_sent_to_others(self):
        self.cog.media = {"v100": "x.gif"}
        self.cog.media_dir = self.root
        (self.root / "x.gif").write_bytes(b"gif")
        self.cog._send_media = ReaderCog._send_media.__get__(self.cog)
        message = self.message("v100")
        message.channel = SimpleNamespace(id=123, send=AsyncMock())

        await self.cog._send_media(message)

        message.channel.send.assert_awaited_once()


class RestrictViewTests(unittest.IsolatedAsyncioTestCase):
    """/restrict のパネル操作。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UserRestrictions(Path(self.tmp.name) / "restrictions.json")
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.bot = SimpleNamespace(restrictions=self.store)
        self.view = RestrictView(self.cog, guild_id=1, owner_id=99)

    def interaction(self, user_id=99):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

    async def test_buttons_are_disabled_until_a_member_is_selected(self):
        for button in self.view.buttons.values():
            self.assertTrue(button.disabled)

        interaction = self.interaction()
        self.view.user_select._values = [SimpleNamespace(id=2)]
        await self.view.on_select_user(interaction)

        self.assertEqual(self.view.selected_user, 2)
        for button in self.view.buttons.values():
            self.assertFalse(button.disabled)
        interaction.response.edit_message.assert_awaited_once()

    async def test_toggle_sets_and_clears_a_flag(self):
        self.view.selected_user = 2
        toggle = self.view.buttons["wav"].callback
        interaction = self.interaction()

        await toggle(interaction)
        self.assertTrue(self.store.blocked(1, 2, "wav"))
        self.assertIn("剥奪中", self.view.buttons["wav"].label)

        await toggle(interaction)
        self.assertFalse(self.store.blocked(1, 2, "wav"))
        self.assertIn("許可", self.view.buttons["wav"].label)

    async def test_clear_removes_everything(self):
        self.store.set_flag(1, 2, "wav", True)
        self.store.set_flag(1, 2, "ignore", True)
        self.view.selected_user = 2

        await self.view.on_clear(self.interaction())

        self.assertEqual(self.store.flags(1, 2), frozenset())

    async def test_only_the_owner_can_operate(self):
        self.view.selected_user = 2
        interaction = self.interaction(user_id=1)

        await self.view.buttons["wav"].callback(interaction)

        self.assertFalse(self.store.blocked(1, 2, "wav"))
        interaction.response.send_message.assert_awaited_once()

    async def test_embed_lists_current_restrictions(self):
        self.store.set_flag(1, 2, "ignore", True)

        embed = self.view.embed()
        names = [field.name for field in embed.fields]
        self.assertIn("制限の内容", names)
        self.assertIn("いま制限中のユーザー", names)
        self.assertIn("<@2>", embed.fields[names.index("いま制限中のユーザー")].value)


if __name__ == "__main__":
    unittest.main()
