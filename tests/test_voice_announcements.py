# ruff: noqa: E501
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class VoiceAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vc_announcements.json"
        self.cache_dir = Path(self.directory.name) / "join_voice"
        # 設定は ReaderCog.config に注入する。
        self.clock = patch("jllm_plugins.reader.core.time.time", return_value=10000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.config.vc_announcement_history_path = str(self.path)
        self.cog.config.vc_announcement_enabled = True
        self.cog.config.vc_announcement_voice = "clone こしあん"
        self.cog.config.vc_announcement_cooldown_seconds = 3600
        self.cog.config.vc_announcement_cache_dir = str(self.cache_dir)
        self.player = SimpleNamespace(
            voice_client=SimpleNamespace(channel=SimpleNamespace(id=10),
                                         is_connected=Mock(return_value=True)),
            skip_users=set(), closed=False,
            enqueue=Mock(return_value=True),
            enqueue_sound=Mock(return_value=True),
        )
        self.cog.players = {1: self.player}
        self.cog.voice_entries = {}
        self.cog.voice_volumes = VoiceVolumes()
        self.cog.voice_speeds = VoiceSpeeds()
        self.cog.readings = SimpleNamespace(apply=lambda text: text.replace("Ryu", "りゅう"))
        self.cog._flatten_speakers = AsyncMock(return_value=[(7, "clone こしあん")])
        self.cog.tts = SimpleNamespace(synthesize=AsyncMock(return_value=b"wav"))
        self.member = SimpleNamespace(id=2, guild=SimpleNamespace(id=1), bot=False,
                                      display_name="Ryu", name="ryu")

    def announcements(self):
        """案内を出した回数（キャッシュ再生と通常の読み上げの合計）。"""

        return self.player.enqueue.call_count + self.player.enqueue_sound.call_count

    async def move(self, before=None, after=10):
        def state(channel):
            return SimpleNamespace(channel=SimpleNamespace(id=channel) if channel else None)
        await self.cog.on_voice_state_update(self.member, state(before), state(after))

    async def test_entry_uses_stable_voice_and_reading_dictionary(self):
        await self.move()
        # 初回は生成して保存し、その音声を再生する
        self.cog.tts.synthesize.assert_awaited_once_with(
            text="りゅうさんが入室しました。", speaker_id="clone こしあん", speed=1.0,
        )
        self.player.enqueue_sound.assert_called_once()
        self.player.enqueue.assert_not_called()
        self.assertEqual(len(list(self.cache_dir.glob("*.wav"))), 1)
        self.assertEqual(json.loads(self.path.read_text()), {"1:2": 10000})

    async def test_the_same_person_and_name_reuses_the_saved_voice(self):
        await self.move()
        # クールダウンを外して、もう一度入り直してもらう
        self.cog.voice_entries = {}
        self.now.return_value = 20000
        await self.move()

        # 生成は1回だけ。2回目は保存済みのwavを再生する
        self.assertEqual(self.cog.tts.synthesize.await_count, 1)
        self.assertEqual(self.player.enqueue_sound.call_count, 2)
        self.assertEqual(len(list(self.cache_dir.glob("*.wav"))), 1)

    async def test_a_name_change_makes_a_new_voice(self):
        await self.move()
        self.cog.voice_entries = {}
        self.now.return_value = 20000
        self.member.display_name = "別の名前"
        await self.move()

        self.assertEqual(self.cog.tts.synthesize.await_count, 2)
        self.assertEqual(len(list(self.cache_dir.glob("*.wav"))), 2)

    async def test_reentry_suppression_tracks_latest_entry_and_hour_boundary(self):
        await self.move()
        self.now.return_value = 11000
        await self.move()
        self.now.return_value = 14600
        await self.move()
        self.assertEqual(self.announcements(), 1)
        self.now.return_value = 18201
        await self.move()
        self.assertEqual(self.announcements(), 2)

    async def test_history_survives_restart(self):
        await self.move()
        self.cog.voice_entries = self.cog._load_voice_entries()
        self.now.return_value = 12000
        await self.move()
        self.assertEqual(self.announcements(), 1)

    async def test_users_and_guilds_have_separate_history(self):
        await self.move()
        self.member.id = 3
        await self.move()
        self.member.guild.id = 4
        self.cog.players[4] = self.player
        await self.move()
        self.assertEqual(self.announcements(), 3)

    async def test_mute_updates_other_channels_and_bots_do_not_announce(self):
        await self.move(10, 10)
        await self.move(None, 11)
        await self.move(11, None)
        self.member.bot = True
        await self.move()
        self.assertEqual(self.announcements(), 0)
        self.assertEqual(self.cog.voice_entries, {})

    async def test_move_into_bot_channel_announces(self):
        await self.move(11, 10)
        self.assertEqual(self.announcements(), 1)

    async def test_leaving_or_moving_out_clears_mute(self):
        for destination in (None, 11):
            self.player.skip_users.add(2)
            await self.move(10, destination)
            self.assertNotIn(2, self.player.skip_users)
        self.assertEqual(self.announcements(), 0)

    async def test_disabled_and_disconnected_do_not_announce(self):
        with patch.object(self.cog.config, "vc_announcement_enabled", False):
            await self.move()
        self.player.voice_client.is_connected.return_value = False
        await self.move()
        self.assertEqual(self.announcements(), 0)
        self.assertFalse(self.path.exists())

    async def test_configured_voice_missing_never_uses_default_voice(self):
        self.cog._flatten_speakers.return_value = [(1, "different voice")]
        await self.move()
        self.cog.tts.synthesize.assert_not_awaited()
        self.assertEqual(self.announcements(), 0)
        self.assertEqual(list(self.cache_dir.glob("*.wav")), [])

    async def test_disconnect_during_voice_lookup_cancels_announcement(self):
        async def disconnected():
            self.player.closed = True
            return [(7, "clone こしあん")]
        self.cog._flatten_speakers.side_effect = disconnected
        await self.move()
        self.cog.tts.synthesize.assert_not_awaited()
        self.assertEqual(self.announcements(), 0)

    async def test_emoji_only_name_uses_username(self):
        self.member.display_name = "🍣🎉"
        await self.move()
        self.cog.tts.synthesize.assert_awaited_once_with(
            text="ryuさんが入室しました。", speaker_id="clone こしあん", speed=1.0,
        )
        self.assertEqual(self.announcements(), 1)

    def test_corrupt_and_expired_history_is_ignored(self):
        self.path.write_text("broken")
        self.assertEqual(self.cog._load_voice_entries(), {})
        self.path.write_text(json.dumps({"1:2": 9900, "expired": 1,
                                         "invalid": "abc", "nan": float("nan")}))
        self.assertEqual(self.cog._load_voice_entries(), {"1:2": 9900})


if __name__ == "__main__":
    unittest.main()
