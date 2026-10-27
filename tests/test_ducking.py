"""空気読み（人が喋っている間だけ音量を下げる）※実験機能。"""

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jllm_plugins.ducking import (
    VOICE_RECV_AVAILABLE,
    SpeechTracker,
    VolumeDucker,
    build_ducker,
    decrypt_dave_packet,
    install_dave_support,
    make_sink,
)
from jllm_plugins.playback.player import _config_of


class SpeechTrackerTests(unittest.TestCase):
    def test_nobody_talking_means_no_ducking(self):
        tracker = SpeechTracker(0.5)

        self.assertFalse(tracker.active(now=100.0))

    def test_stays_active_for_the_hold_window(self):
        tracker = SpeechTracker(0.5)
        tracker.touch(7)

        now = tracker.last_spoke

        self.assertTrue(tracker.active(now=now))
        self.assertTrue(tracker.active(now=now + 0.4))
        self.assertFalse(tracker.active(now=now + 0.6))
        self.assertEqual(tracker.speakers, {7: tracker.last_spoke})

    def test_reset_clears_everything(self):
        tracker = SpeechTracker(0.5)
        tracker.touch(7)
        tracker.reset()

        self.assertFalse(tracker.active())
        self.assertEqual(tracker.speakers, {})


class VolumeDuckerTests(unittest.IsolatedAsyncioTestCase):
    def make(self, level=0.35):
        from jllm_plugins.ducking.duck_curve import DuckCurve, Point

        applied = []
        tracker = SpeechTracker(0.2)
        ducker = VolumeDucker(
            tracker,
            applied.append,
            tick_seconds=0.01,
            attack=0.5,
            release=0.25,
            curve=DuckCurve([Point(0.0, level)]),
        )

        return tracker, ducker, applied

    def test_ducking_approaches_the_level_smoothly(self):
        tracker, ducker, _ = self.make()
        tracker.touch(1)

        first = ducker.step(True)
        second = ducker.step(True)

        self.assertLess(first, 1.0)
        self.assertLess(second, first)  # 少しずつ下がる（急に切り替えない）
        self.assertGreater(first, 0.35)

        for _ in range(20):
            ducker.step(True)

        self.assertAlmostEqual(ducker.volume, 0.35, places=2)

    def test_returns_to_full_volume(self):
        tracker, ducker, _ = self.make()
        tracker.touch(1)

        for _ in range(20):
            ducker.step(True)

        # 戻すのはゆっくりなので、それなりのtick数がかかる
        for _ in range(200):
            ducker.step(False)

        self.assertAlmostEqual(ducker.volume, 1.0, places=2)

    async def test_run_and_stop_restore_the_volume(self):
        tracker, ducker, applied = self.make(level=0.2)
        tracker.touch(1)
        ducker.start()
        await asyncio.sleep(0.08)
        await ducker.stop()

        self.assertTrue(applied)
        self.assertLess(min(applied), 0.5)  # 下がった
        self.assertEqual(applied[-1], 1.0)  # 最後は元に戻す
        self.assertIsNone(ducker._task)


class DurationTests(unittest.TestCase):
    """喋っている秒数はトラッカーが持ち、反応の遅れはカーブが決める。"""

    def make(self, **kwargs):
        return SpeechTracker(0.8, **kwargs)

    def feed(self, tracker, seconds, start=1000.0, step=0.02, user_id=None):
        for index in range(int(seconds / step)):
            tracker.observe_packet(user_id, now=start + index * step)

    def test_speaking_seconds_counts_up(self):
        tracker = self.make()
        self.feed(tracker, 1.2)

        self.assertAlmostEqual(tracker.speaking_seconds(now=1001.2), 1.2, places=2)
        self.assertTrue(tracker.speaking)
        self.assertTrue(tracker.receiving(now=1001.2))

    def test_short_speech_is_still_speaking(self):
        # 短い相づちでも「喋っている」にはなる（下げるかどうかはカーブ次第）
        tracker = self.make()
        self.feed(tracker, 0.3, user_id=5)

        self.assertTrue(tracker.speaking)
        self.assertIn(5, tracker.speakers)

    def test_a_long_pause_resets_the_count(self):
        tracker = self.make()
        self.feed(tracker, 1.0)
        tracker.observe_packet(now=1002.0)

        self.assertAlmostEqual(tracker.speaking_seconds(now=1002.5), 0.0)
        self.assertFalse(tracker.receiving(now=1002.5))

    def test_hold_keeps_it_active_after_the_last_packet(self):
        tracker = self.make()
        self.feed(tracker, 1.0)

        self.assertTrue(tracker.active(now=1001.5))
        self.assertFalse(tracker.active(now=1002.0))


class CurveTriggerTests(unittest.TestCase):
    """カーブの区間が「反応するまで」と「下げ幅」を決める。"""

    def make(self, points, **kwargs):
        from jllm_plugins.ducking.duck_curve import DuckCurve, Point

        tracker = SpeechTracker(0.8)
        curve = DuckCurve([Point(*item) for item in points], **kwargs)
        ducker = VolumeDucker(tracker, lambda volume: None, tick_seconds=0.04, curve=curve)

        return tracker, ducker

    def run_for(self, tracker, ducker, seconds, start=1000.0):
        for index in range(int(seconds / 0.04)):
            now = start + index * 0.04
            tracker.observe_packet(now=now)
            ducker.step(True, now)

        return ducker.volume

    def test_reaction_delay_comes_from_the_first_change(self):
        tracker, ducker = self.make([(0.0, 1.0), (1.0, 0.2)], attack=1.0)

        self.assertAlmostEqual(self.run_for(tracker, ducker, 0.9), 1.0, places=2)
        self.assertLess(self.run_for(tracker, ducker, 0.2, start=1000.9), 1.0)

    def test_deeper_segments_apply_the_longer_they_talk(self):
        tracker, ducker = self.make([(0.0, 1.0), (1.0, 0.4), (3.0, 0.2)], attack=1.0)

        self.assertAlmostEqual(self.run_for(tracker, ducker, 1.2), 0.4, places=2)
        self.assertAlmostEqual(self.run_for(tracker, ducker, 2.0, start=1001.2), 0.2, places=2)

    def test_target_returns_to_full_when_quiet(self):
        tracker, ducker = self.make([(0.0, 1.0), (0.0, 0.2)])
        tracker.observe_packet(now=1000.0)

        self.assertAlmostEqual(ducker.target(now=1000.0), 0.2, places=2)
        # 喋り終わって保持を過ぎたら元の音量に戻す
        self.assertAlmostEqual(ducker.target(now=1000.0 + 2.0), 1.0, places=2)

    def test_set_curve_applies_immediately(self):
        from jllm_plugins.ducking.duck_curve import DuckCurve, Point

        tracker, ducker = self.make([(0.0, 1.0), (0.0, 0.5)])

        self.assertAlmostEqual(ducker.level, 0.5)
        ducker.set_curve(DuckCurve([Point(0.0, 0.1)], attack=0.5, release=0.2))
        self.assertAlmostEqual(ducker.level, 0.1)
        self.assertAlmostEqual(ducker.attack, 0.5)
        self.assertAlmostEqual(ducker.release, 0.2)


class SinkTests(unittest.TestCase):
    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_packets_mark_speech(self):
        tracker = SpeechTracker(0.5)
        sink = make_sink(tracker)

        # 既定は opus のまま（音量を見ないのでデコード不要）
        self.assertTrue(sink.wants_opus())
        sink.write(SimpleNamespace(id=5, bot=False), SimpleNamespace(pcm=b""))

        self.assertTrue(tracker.speaking)
        self.assertIn(5, tracker.speakers)

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_opus_path_never_decodes_the_audio(self):
        """デコード無しの経路で正しく喋りを拾える。

        sink は opus のまま受け取るので、``PacketDecoder`` は Decoder を作らず、
        DAVE（E2EE）や壊れたパケットでもデコードで例外にならない。
        """

        from discord.ext.voice_recv import opus as vr_opus

        tracker = SpeechTracker(0.5)
        sink = make_sink(tracker)
        sink._voice_client = SimpleNamespace(
            guild=SimpleNamespace(get_member=lambda uid: SimpleNamespace(id=uid, bot=False)),
            client=SimpleNamespace(get_user=lambda uid: None),
            _get_id_from_ssrc=lambda ssrc: 5,
        )
        decoder = vr_opus.PacketDecoder(SimpleNamespace(sink=sink), 1234)

        self.assertIsNone(decoder._decoder)  # 復号器を持たない

        def boom(packet):
            raise AssertionError("デコードしてはいけない")

        decoder._decode_packet = boom
        packet = SimpleNamespace(sequence=1, timestamp=0, decrypted_data=b"not opus")
        data = decoder._process_packet(packet)

        self.assertEqual(data.pcm, b"")  # PCM は作らない
        sink.write(data.source, data)
        self.assertTrue(tracker.speaking)
        self.assertIn(5, tracker.speakers)

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_bot_audio_is_ignored(self):
        tracker = SpeechTracker(0.5)
        sink = make_sink(tracker)

        sink.write(SimpleNamespace(id=9, bot=True), SimpleNamespace(pcm=b""))

        self.assertFalse(tracker.speaking)


class BuildDuckerTests(unittest.IsolatedAsyncioTestCase):
    def test_without_voice_recv_it_does_nothing(self):
        with patch("jllm_plugins.ducking.VOICE_RECV_AVAILABLE", False):
            self.assertIsNone(build_ducker(Mock(), Mock(), level=0.3, hold_seconds=0.5))

    def test_with_a_plain_voice_client_it_does_nothing(self):
        self.assertIsNone(build_ducker(SimpleNamespace(), Mock(), level=0.3, hold_seconds=0.5))
        self.assertIsNone(build_ducker(None, Mock(), level=0.3, hold_seconds=0.5))

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    async def test_with_a_recv_client_it_starts_and_stops(self):
        from discord.ext import voice_recv

        client = voice_recv.VoiceRecvClient.__new__(voice_recv.VoiceRecvClient)
        client.listen = Mock()
        applied = []

        ducker = build_ducker(client, applied.append, level=0.3, hold_seconds=0.1)

        self.assertIsNotNone(ducker)
        client.listen.assert_called_once()
        await asyncio.sleep(0.05)
        await ducker.stop()
        self.assertEqual(applied[-1], 1.0)


class PlayerDuckTests(unittest.IsolatedAsyncioTestCase):
    def make_player(self):
        from jllm_plugins.playback.player import SpeechPlayer

        player = SpeechPlayer.__new__(SpeechPlayer)
        player.ducker = None
        player.current_source = SimpleNamespace(volume=1.0)
        player.voice_client = SimpleNamespace()
        player.closed = False

        return player

    def test_set_duck_changes_the_current_source(self):
        player = self.make_player()

        player.set_duck(0.35)
        self.assertAlmostEqual(player.current_source.volume, 0.35)
        self.assertAlmostEqual(player.duck_volume, 0.35)

        player.set_duck(2.0)  # 1.0 を超えない
        self.assertAlmostEqual(player.current_source.volume, 1.0)

        player.set_duck(-1)  # 0 を下回らない
        self.assertAlmostEqual(player.current_source.volume, 0.0)

    def test_set_duck_without_playing_does_not_fail(self):
        player = self.make_player()
        player.current_source = None

        player.set_duck(0.4)

        self.assertAlmostEqual(player.duck_volume, 0.4)

    async def test_enable_listening_is_off_by_default(self):
        player = self.make_player()
        player.closed = False

        player.config = replace(_config_of(player), duck_enabled=False)
        await player.enable_listening()

        self.assertIsNone(player.ducker)

    async def test_enable_listening_uses_the_builder(self):
        player = self.make_player()
        fake = SimpleNamespace(stop=Mock())

        player.config = replace(
            _config_of(player),
            duck_enabled=True,
            duck_level=0.25,
            duck_trigger_seconds=1.0,
        )
        with patch("jllm_plugins.ducking.build_ducker", return_value=fake) as builder:
            await player.enable_listening()

        self.assertIs(player.ducker, fake)
        builder.assert_called_once()
        self.assertAlmostEqual(builder.call_args.kwargs["level"], 0.25)
        self.assertAlmostEqual(builder.call_args.kwargs["trigger_seconds"], 1.0)


class DecodeSinkTests(unittest.TestCase):
    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_decode_sink_wants_pcm_and_passes_it_on(self):
        tracker = SpeechTracker(0.5)
        heard = []
        sink = make_sink(tracker, decode=True, on_audio=lambda uid, pcm: heard.append((uid, pcm)))

        self.assertFalse(sink.wants_opus())
        sink.write(SimpleNamespace(id=5, bot=False), SimpleNamespace(pcm=b"\x01\x02"))

        self.assertEqual(heard, [(5, b"\x01\x02")])
        self.assertTrue(tracker.speaking)

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_opus_sink_counts_packets(self):
        tracker = SpeechTracker(0.5)
        sink = make_sink(tracker)

        sink.write(SimpleNamespace(id=5, bot=False), SimpleNamespace(pcm=b""))
        sink.write(SimpleNamespace(id=5, bot=False), SimpleNamespace(pcm=b""))

        self.assertEqual(tracker.packets, 2)


class DaveSupportTests(unittest.TestCase):
    def test_decrypt_dave_packet_decrypts_when_ready(self):
        class Session:
            ready = True

            def decrypt(self, user_id, media, data):
                self.called = (user_id, media, data)
                return b"plain"

        session = Session()
        self.assertEqual(decrypt_dave_packet(session, 5, b"cipher"), b"plain")
        self.assertEqual(session.called[0], 5)
        self.assertEqual(session.called[2], b"cipher")

    def test_decrypt_dave_packet_gives_up_safely(self):
        class NotReady:
            ready = False

            def decrypt(self, *args):  # pragma: no cover 呼ばれない
                raise AssertionError("ready でないのに復号した")

        class Broken:
            ready = True

            def decrypt(self, *args):
                raise RuntimeError("壊れたパケット")

        self.assertIsNone(decrypt_dave_packet(None, 5, b"cipher"))
        self.assertIsNone(decrypt_dave_packet(NotReady(), 5, b"cipher"))
        self.assertIsNone(decrypt_dave_packet(Broken(), 5, b"cipher"))
        self.assertIsNone(decrypt_dave_packet(NotReady(), None, b"cipher"))
        self.assertIsNone(decrypt_dave_packet(NotReady(), 5, b""))

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_install_dave_support_patches_decoder_and_packet_decoder(self):
        from discord.ext.voice_recv import opus as vr_opus

        class DummyDecoder:
            def decode(self, data, *, fec=False):
                return b"pcm"

        class DummyPacketDecoder:
            def _process_packet(self, packet):
                packet.processed = True
                return packet

        with (
            patch.object(vr_opus, "Decoder", DummyDecoder),
            patch.object(vr_opus, "PacketDecoder", DummyPacketDecoder),
        ):
            self.assertTrue(install_dave_support())

            # 壊れたパケットは例外にせず無音にする
            safe = vr_opus.Decoder
            self.assertTrue(safe._voice_recv_safe_decode)

            def boom(data, *, fec=False):
                raise RuntimeError("壊れたパケット")

            DummyDecoder.decode = boom
            self.assertEqual(safe().decode(b"x"), b"")

            # DAVE の復号が _process_packet にはさまり、元の処理も走る
            self.assertTrue(DummyPacketDecoder._voice_recv_dave)

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    def test_dave_patch_decrypts_the_packet_before_processing(self):
        from discord.ext.voice_recv import opus as vr_opus

        class DummyDecoder:
            def decode(self, data, *, fec=False):
                return b"pcm"

        class Original:
            def _process_packet(self, packet):
                return packet

        seen = []

        class Session:
            ready = True

            def decrypt(self, user_id, media, data):
                seen.append((user_id, data))
                return b"decrypted"

        with (
            patch.object(vr_opus, "Decoder", DummyDecoder),
            patch.object(vr_opus, "PacketDecoder", Original),
        ):
            install_dave_support()

            decoder = Original()
            decoder._cached_id = 7
            decoder.sink = SimpleNamespace(
                voice_client=SimpleNamespace(
                    _connection=SimpleNamespace(dave_session=Session()),
                ),
            )
            packet = SimpleNamespace(decrypted_data=b"encrypted")

            result = Original._process_packet(decoder, packet)

        self.assertEqual(seen, [(7, b"encrypted")])
        self.assertEqual(result.decrypted_data, b"decrypted")


class BuildDuckerDecodeTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    async def test_build_ducker_only_installs_dave_when_decoding(self):
        from discord.ext import voice_recv

        def make_client():
            client = voice_recv.VoiceRecvClient.__new__(voice_recv.VoiceRecvClient)
            client.listen = Mock()
            return client

        with patch("jllm_plugins.ducking.install_dave_support") as installer:
            ducker = build_ducker(make_client(), Mock(), level=0.3, hold_seconds=0.1)
            installer.assert_not_called()
            await ducker.stop()

            decoder = build_ducker(make_client(), Mock(), level=0.3, hold_seconds=0.1, decode=True)
            installer.assert_called_once()
            await decoder.stop()


if __name__ == "__main__":
    unittest.main()
