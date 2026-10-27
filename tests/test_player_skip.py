import asyncio
import io
import shutil
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.playback.player import AudioChunk, SpeechItem, SpeechPlayer


def tiny_wav(frames: bytes) -> bytes:
    output = io.BytesIO()

    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(frames)

    return output.getvalue()


def frame_count(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as reader:
        return reader.getnframes()


def make_player():
    player = SpeechPlayer.__new__(SpeechPlayer)
    player.closed = False
    player.generation = 0
    player.voice_client = SimpleNamespace(
        playing=True,
        stopped=0,
        is_playing=lambda: player.voice_client.playing,
        is_paused=lambda: False,
        stop=lambda: (
            setattr(player.voice_client, "playing", False),
            setattr(player.voice_client, "stopped", player.voice_client.stopped + 1),
        ),
        is_connected=lambda: True,
    )
    player.queue = asyncio.Queue()
    player.audio_queue = asyncio.Queue()
    player.current = None
    player.pending = None
    player.dropped_posts = {}
    return player


def speech(post_id, author_key, text="あ"):
    return SpeechItem(
        text=text,
        speaker_id=1,
        speed=1.0,
        post_id=post_id,
        author_key=author_key,
    )


class SkipCurrentPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_drops_current_post_and_same_author_queued(self):
        player = make_player()
        player.current = AudioChunk("p1", "g:1", b"RIFFnow")
        player.queue.put_nowait(speech("p1", "g:1"))
        player.queue.put_nowait(speech("p2", "g:1"))
        player.queue.put_nowait(speech("p3", "g:2"))
        player.audio_queue.put_nowait(AudioChunk("p2", "g:1", b"RIFFq"))
        player.audio_queue.put_nowait(AudioChunk("p4", "g:2", b"RIFFkeep"))
        self.assertTrue(player.skip_current_post())
        self.assertEqual(player.voice_client.stopped, 1)
        self.assertIn("p1", player.dropped_posts)
        self.assertIn("p2", player.dropped_posts)
        self.assertNotIn("p3", player.dropped_posts)
        self.assertNotIn("p4", player.dropped_posts)
        self.assertEqual(player.generation, 0)

    async def test_idle_with_queues_drops_head_author(self):
        player = make_player()
        player.voice_client.playing = False
        player.audio_queue.put_nowait(AudioChunk("p9", "g:7", b"RIFFq"))
        self.assertTrue(player.skip_current_post())
        self.assertIn("p9", player.dropped_posts)

    async def test_nothing_to_skip(self):
        player = make_player()
        player.voice_client.playing = False
        self.assertFalse(player.skip_current_post())

    async def test_skip_still_drains_everything(self):
        player = make_player()
        player.queue.put_nowait(speech("p1", "g:1"))
        player.audio_queue.put_nowait(AudioChunk("p2", "g:2", b"RIFFq"))
        self.assertTrue(player.skip())
        self.assertEqual(player.queue.qsize(), 0)
        self.assertEqual(player.audio_queue.qsize(), 0)
        self.assertEqual(player.generation, 1)

    async def test_enqueue_carries_post_and_volume(self):
        player = make_player()
        player.speaker_id = 1
        self.assertTrue(player.enqueue_sound("a.wav", 1.0, 0.4, "p1", "g:1"))
        item = player.queue.get_nowait()
        self.assertEqual(
            (item.sound_path, item.volume, item.post_id, item.author_key),
            ("a.wav", 0.4, "p1", "g:1"),
        )
        self.assertTrue(player.enqueue("hi", None, 2.0, "p2", "g:1"))
        item = player.queue.get_nowait()
        self.assertEqual(item.speed, 2.0)
        self.assertEqual(item.post_id, "p2")


class PlayLoopGroupingTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_post_combined_other_post_separate(self):
        player = make_player()
        played = []
        player._active = lambda: True

        async def fake_play(audio):
            played.append(audio)

        player._play = fake_play
        unit = tiny_wav(b"\x01\x00" * 48)
        player.audio_queue.put_nowait(AudioChunk("p1", "g:1", unit))
        player.audio_queue.put_nowait(AudioChunk("p1", "g:1", unit))
        player.audio_queue.put_nowait(AudioChunk("p2", "g:2", unit))
        task = asyncio.create_task(player._play_loop())
        await asyncio.sleep(0.2)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertEqual(len(played), 2)
        self.assertEqual(frame_count(played[0]) - frame_count(played[1]), 48)

    async def test_dropped_chunks_are_skipped(self):
        player = make_player()
        played = []
        player._active = lambda: True

        async def fake_play(audio):
            played.append(audio)

        player._play = fake_play
        unit = tiny_wav(b"\x01\x00" * 48)
        player.audio_queue.put_nowait(AudioChunk("p1", "g:1", unit))
        player.audio_queue.put_nowait(AudioChunk("p2", "g:2", unit))
        player.dropped_posts["p1"] = None
        task = asyncio.create_task(player._play_loop())
        await asyncio.sleep(0.2)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertEqual(len(played), 1)
        self.assertGreater(frame_count(played[0]), 0)


class TtsVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def run_synth(self, player_volume, item_volume):
        player = make_player()
        unit = tiny_wav(b"\xe8\x03" * 48)  # 1000 per sample
        player.tts = SimpleNamespace(synthesize=self._synth(unit))
        player.volume = player_volume
        player.queue.put_nowait(
            SpeechItem(
                text="あ",
                speaker_id=2,
                speed=1.0,
                volume=item_volume,
                post_id="p1",
                author_key="g:1",
            )
        )
        task = asyncio.create_task(player._synthesize_loop())
        chunk = await asyncio.wait_for(player.audio_queue.get(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return chunk.audio

    @staticmethod
    def _synth(unit):
        async def synthesize(text, speaker_id, speed=1.0):
            return unit

        return synthesize

    @staticmethod
    def peak(audio):
        with wave.open(io.BytesIO(audio), "rb") as reader:
            frames = reader.readframes(reader.getnframes())
        samples = [
            int.from_bytes(frames[i : i + 2], "little", signed=True)
            for i in range(0, len(frames), 2)
        ]
        return max(abs(sample) for sample in samples)

    async def test_guild_and_voice_gains_multiply(self):
        audio = await self.run_synth(player_volume=0.5, item_volume=2.0)
        self.assertEqual(self.peak(audio), 1000)  # 0.5 * 2.0 == 1.0: unchanged

    async def test_voice_gain_above_unity_amplifies_with_clipping(self):
        audio = await self.run_synth(player_volume=0.5, item_volume=3.0)
        self.assertEqual(self.peak(audio), 1500)

    async def test_clipping_at_int16_limits(self):
        player = make_player()
        unit = tiny_wav(b"\xff\x7f" * 48)  # 32767 per sample
        player.tts = SimpleNamespace(synthesize=self._synth(unit))
        player.volume = 1.0
        player.queue.put_nowait(
            SpeechItem(
                text="あ",
                speaker_id=2,
                speed=1.0,
                volume=3.0,
                post_id="p1",
                author_key="g:1",
            )
        )
        task = asyncio.create_task(player._synthesize_loop())
        chunk = await asyncio.wait_for(player.audio_queue.get(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(self.peak(chunk.audio), 32767)


class TtsEffectsTests(unittest.IsolatedAsyncioTestCase):
    async def run_synth(self, effects, apply_mock):
        player = make_player()
        unit = tiny_wav(b"\xe8\x03" * 48)
        player.tts = SimpleNamespace(synthesize=TtsVolumeTests._synth(unit))
        player.volume = 1.0
        player.queue.put_nowait(
            SpeechItem(
                text="あ",
                speaker_id=2,
                speed=1.0,
                volume=1.0,
                effects=effects,
                post_id="p1",
                author_key="g:1",
            )
        )
        with patch.object(SpeechPlayer, "_apply_effects", apply_mock):
            task = asyncio.create_task(player._synthesize_loop())
            chunk = await asyncio.wait_for(player.audio_queue.get(), timeout=5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return chunk.audio

    async def test_effects_applied_between_synth_and_volume(self):
        apply = Mock(side_effect=lambda audio, fx, *_: audio + b"+fx")
        audio = await self.run_synth((("rb", 2.0),), apply)
        apply.assert_called_once()
        call_audio, call_effects = apply.call_args.args[:2]
        self.assertEqual(call_effects, (("rb", 2.0),))
        self.assertTrue(call_audio.startswith(b"RIFF"))
        self.assertTrue(audio.endswith(b"+fx"))

    async def test_empty_effects_skip_ffmpeg(self):
        apply = Mock(side_effect=lambda audio, fx, *_: audio + b"+fx")
        unit = tiny_wav(b"\xe8\x03" * 48)
        audio = await self.run_synth((), apply)
        apply.assert_not_called()
        self.assertEqual(audio, unit)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
    def test_broken_filter_falls_back_to_original(self):
        from jllm_plugins.playback.player import SpeechPlayer

        unit = tiny_wav(b"\xe8\x03" * 48)
        with patch("jllm_plugins.playback.player.build_filter", return_value="!!!invalid!!!"):
            self.assertEqual(SpeechPlayer._apply_effects(unit, (("rb", 1.0),)), unit)


class TtsServerSpeedSplitTests(unittest.IsolatedAsyncioTestCase):
    async def run_synth(self, speed):
        player = make_player()
        unit = tiny_wav(b"\xe8\x03" * 48)
        synth = AsyncMock(return_value=unit)
        player.tts = SimpleNamespace(synthesize=synth)
        player.volume = 1.0
        player.queue.put_nowait(
            SpeechItem(
                text="あ",
                speaker_id=2,
                speed=speed,
                volume=1.0,
                post_id="p1",
                author_key="g:1",
            )
        )
        with patch.object(SpeechPlayer, "_apply_speed_bytes", return_value=unit) as applied:
            task = asyncio.create_task(player._synthesize_loop())
            chunk = await asyncio.wait_for(player.audio_queue.get(), timeout=5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return synth, applied, chunk.audio

    async def test_extreme_speed_splits_server_and_local(self):
        synth, applied, _ = await self.run_synth(20.0)
        self.assertEqual(synth.call_args.kwargs["speed"], 4.0)
        applied.assert_called_once()
        self.assertAlmostEqual(applied.call_args.args[1], 5.0)

    async def test_in_range_speed_goes_directly(self):
        synth, applied, _ = await self.run_synth(2.0)
        self.assertEqual(synth.call_args.kwargs["speed"], 2.0)
        applied.assert_not_called()

    async def test_slow_speed_splits(self):
        synth, applied, _ = await self.run_synth(0.1)
        self.assertEqual(synth.call_args.kwargs["speed"], 0.25)
        self.assertAlmostEqual(applied.call_args.args[1], 0.4)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
    def test_local_remainder_actually_changes_duration(self):
        from jllm_plugins.playback.player import SpeechPlayer

        unit = tiny_wav(b"\xe8\x03" * 480)
        with wave.open(io.BytesIO(unit), "rb") as reader:
            before = reader.getnframes()
        after = SpeechPlayer._apply_speed_bytes(unit, 5.0)
        self.assertIsNotNone(after)
        with wave.open(io.BytesIO(after), "rb") as reader:
            frames = reader.getnframes()
        self.assertLess(frames, before // 4)


if __name__ == "__main__":
    unittest.main()
