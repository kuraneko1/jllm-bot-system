"""音声接続まわりの回帰テスト（受信を止めない・送信を見える化する）。"""

import asyncio
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.ducking import SpeechTracker
from jllm_plugins.playback.player import SpeechPlayer, _config_of, install_send_stats


class FakeVoiceClient:
    """VoiceRecvClient と同じく、stop() で受信も止まる接続。"""

    def __init__(self):
        self.reader_alive = True
        self.playing = False
        self._connection = SimpleNamespace(can_encrypt=True)

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def stop_playing(self):
        self.playing = False

    def stop(self):
        self.playing = False
        self.reader_alive = False  # VoiceRecvClient.stop() は stop_listening() も呼ぶ

    def _get_voice_packet(self, data):
        return b"packet"


def make_player(voice_client=None):
    player = SpeechPlayer.__new__(SpeechPlayer)
    player.voice_client = voice_client or FakeVoiceClient()
    player.guild_id = 1
    player.ducker = None
    player.current_source = None
    player.duck_volume = 1.0
    player.closed = False
    player.generation = 0
    player.send_stats = {}
    return player


class StopPlaybackTests(unittest.TestCase):
    def test_stop_playback_keeps_receiving_audio(self):
        """再生だけ止める（VoiceRecvClient.stop() で空気読みを殺さない）。"""

        client = FakeVoiceClient()
        client.playing = True
        player = make_player(client)

        player._stop_playback()

        self.assertFalse(client.playing)
        self.assertTrue(client.reader_alive)  # 受信は生きている

    def test_stop_playback_falls_back_to_stop(self):
        client = SimpleNamespace(stopped=0, stop=lambda: setattr(client, "stopped", 1))
        player = make_player(client)

        player._stop_playback()

        self.assertEqual(client.stopped, 1)

    def test_skip_keeps_receiving_audio(self):
        client = FakeVoiceClient()
        client.playing = True
        player = make_player(client)
        player.queue = __import__("asyncio").Queue()
        player.audio_queue = __import__("asyncio").Queue()
        player.pending = None
        player.dropped_posts = {}

        self.assertTrue(player.skip())
        self.assertTrue(client.reader_alive)


class SendStatsTests(unittest.TestCase):
    def test_counts_sent_packets(self):
        client = FakeVoiceClient()
        stats = install_send_stats(client)

        client._get_voice_packet(b"a")
        client._get_voice_packet(b"b")

        self.assertEqual(stats["packets"], 2)
        self.assertEqual(stats["bytes"], 12)
        self.assertEqual(stats["encrypted"], 2)
        self.assertGreater(stats["last"], 0.0)

    def test_is_idempotent(self):
        client = FakeVoiceClient()

        self.assertIs(install_send_stats(client), install_send_stats(client))

    def test_without_the_hook_it_reports_nothing(self):
        self.assertEqual(install_send_stats(SimpleNamespace()), {})


class VoiceStatusTests(unittest.TestCase):
    def make_status_client(self):
        return SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_listening=lambda: True,
            channel=SimpleNamespace(id=42),
            guild=SimpleNamespace(
                me=SimpleNamespace(
                    voice=SimpleNamespace(self_mute=False, self_deaf=False, mute=False, deaf=False),
                ),
            ),
            _connection=SimpleNamespace(
                dave_protocol_version=1,
                can_encrypt=True,
                dave_session=SimpleNamespace(ready=True),
                dave_downgraded=False,
            ),
        )

    def test_reports_dave_mute_and_counters(self):
        player = make_player(self.make_status_client())
        tracker = SpeechTracker(0.5)
        tracker.observe_packet(5)
        player.ducker = SimpleNamespace(tracker=tracker)
        player.send_stats = {
            "packets": 3,
            "bytes": 300,
            "encrypted": 3,
            "last": time.monotonic(),
        }

        status = player.voice_status()

        self.assertTrue(status["connected"])
        self.assertEqual(status["channel_id"], "42")
        self.assertTrue(status["listening"])
        self.assertFalse(status["server_mute"])
        self.assertEqual(status["dave"]["protocol_version"], 1)
        self.assertTrue(status["dave"]["can_encrypt"])
        self.assertTrue(status["dave"]["session_ready"])
        self.assertEqual(status["send"]["packets"], 3)
        self.assertEqual(status["receive"]["packets"], 1)
        self.assertIsNotNone(status["receive"]["seconds_since_last"])

    def test_works_without_a_ducker(self):
        player = make_player(self.make_status_client())

        status = player.voice_status()

        self.assertIsNone(status["receive"]["packets"])
        self.assertTrue(status["connected"])


class ReconnectVoiceTests(unittest.IsolatedAsyncioTestCase):
    def make_channel(self, new_client):
        channel = SimpleNamespace(
            id=42,
            connect=AsyncMock(return_value=new_client),
        )
        channel.guild = SimpleNamespace(get_channel=lambda cid: channel)
        return channel

    async def test_reconnect_voice_rejoins_the_same_channel(self):
        new_client = SimpleNamespace(channel=SimpleNamespace(id=42))
        channel = self.make_channel(new_client)
        old = SimpleNamespace(
            channel=channel,
            guild=channel.guild,
            disconnect=AsyncMock(),
            is_connected=lambda: False,
        )
        player = make_player(old)
        player.channel_id = 42

        player.config = replace(_config_of(player), duck_enabled=False)
        self.assertTrue(await player.reconnect_voice())

        self.assertIs(player.voice_client, new_client)
        channel.connect.assert_awaited_once()

    async def test_reconnect_voice_keeps_the_old_client_on_failure(self):
        channel = self.make_channel(None)
        channel.connect = AsyncMock(side_effect=RuntimeError("つながらない"))
        old = SimpleNamespace(
            channel=channel,
            guild=channel.guild,
            disconnect=AsyncMock(),
            is_connected=lambda: False,
        )
        player = make_player(old)
        player.channel_id = 42

        self.assertFalse(await player.reconnect_voice())
        self.assertIs(player.voice_client, old)

    async def test_reconnect_voice_without_a_channel_does_nothing(self):
        old = SimpleNamespace(channel=None, guild=None, disconnect=AsyncMock())
        player = make_player(old)
        player.channel_id = None

        self.assertFalse(await player.reconnect_voice())

    async def test_watchdog_reconnects_after_the_grace(self):
        client = SimpleNamespace(is_connected=lambda: False)
        player = make_player(client)
        player.reconnect_voice = AsyncMock(return_value=True)

        with (
            patch("jllm_plugins.playback.player.VOICE_WATCHDOG_INTERVAL", 0.01),
            patch("jllm_plugins.playback.player.VOICE_RECONNECT_GRACE", 0.0),
        ):
            task = asyncio.create_task(player._watch_voice())
            await asyncio.sleep(0.1)
            player.closed = True
            await asyncio.wait_for(task, timeout=2)

        player.reconnect_voice.assert_awaited()


if __name__ == "__main__":
    unittest.main()
