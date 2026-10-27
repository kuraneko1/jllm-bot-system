"""ストリーミング再生（services/player.py の生 PCM 経路）。

drama3-stream が `response_format=pcm` + `streaming=true` で返す s16le を、
ffmpeg のパイプへ**届いた順に**流せるか。ここが詰まると初音が遅れるか、
音が途切れる（2026-10-18 の「ストリーミング再生がうまくいかない」）。
"""

import asyncio
import os
import unittest
from types import SimpleNamespace

from jllm_plugins.playback.player import STREAM_RATE, SpeechItem, SpeechPlayer, StreamingAudio
from jllm_tts import TTSBackendError, TTSContentError


def make_player(volume: float = 0.2):
    """Discord に繋がずに組み立てる（既存の test_player_skip と同じ流儀）。"""

    player = SpeechPlayer.__new__(SpeechPlayer)
    player.closed = False
    player.generation = 0
    player.voice_client = SimpleNamespace(is_connected=lambda: True)
    player.queue = asyncio.Queue()
    player.audio_queue = asyncio.Queue()
    player.current = None
    player.pending = None
    player.dropped_posts = {}
    player.volume = volume
    player.sample_volume = volume
    player.duck_volume = 1.0
    player.current_gain = 1.0
    player.current_source = None

    return player


def speech(text="あ", speed=1.0, volume=1.0, effects=()):
    return SpeechItem(
        text=text,
        speaker_id="test",
        speed=speed,
        volume=volume,
        effects=tuple(effects),
    )


class FakeStreamBackend:
    def __init__(self, chunks, error: Exception | None = None):
        self.chunks = chunks
        self.error = error
        self.supports_streaming = True
        self.closed = False

    async def stream(self, text, speaker_id, speed=1.0):
        for chunk in self.chunks:
            yield chunk

        if self.error is not None:
            raise self.error

    def aclose_probe(self):
        return self.closed


class StreamEligibilityTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        player = make_player()
        player.tts = FakeStreamBackend([b"aa"])
        return player

    async def test_a_plain_line_streams(self):
        player = self.make()

        self.assertTrue(player._can_stream(speech()))

    async def test_effects_and_speed_need_the_whole_wav(self):
        player = self.make()

        self.assertFalse(player._can_stream(speech(speed=1.5)))
        self.assertFalse(player._can_stream(speech(effects=("reverb",))))

    async def test_a_server_side_speed_still_streams(self):
        # 速度をサーバが掛けるバックエンド（Irodori 等）は、等速でなくても鳴らし始める。
        # 手元 atempo が要るバックエンドは wav を作る（全体が要る）。
        backend = FakeStreamBackend([b"head"])
        backend.streams_any_speed = True
        backend.resolve_speed = lambda speed: speed
        player = make_player()
        player.tts = backend

        self.assertTrue(player._can_stream(speech(speed=1.2)))

    async def test_a_backend_without_streaming_is_not_used(self):
        player = self.make()
        player.tts.supports_streaming = False

        self.assertFalse(player._can_stream(speech()))

    async def test_too_loud_a_gain_is_left_to_the_wav_path(self):
        # PCMVolumeTransformer は 2.0 で頭打ちなので、それを超えるなら wav で焼く
        player = self.make()

        # プレイヤー側の既定音量は 0.2 なので、12 倍で 2.4 になる
        self.assertFalse(player._can_stream(speech(volume=12.0)))
        self.assertTrue(player._can_stream(speech(volume=10.0)))


class RouterStreamTests(unittest.IsolatedAsyncioTestCase):
    """本番の形（``build_tts()`` が返す TTSRouter）越しでもストリーミングできるか。

    2026-10-18: ルーターが ``stream`` を持たず、本番では一度も逐次再生されていなかった。
    """

    def make(self, *, ignore_speed=False, mode=None):
        import tempfile
        from pathlib import Path

        from jllm_plugins.tts_router.router import BackendSpec, TTSRouter

        backend = FakeStreamBackend([b"head", b"tail"])
        backend.ignore_speaker_speed = ignore_speed
        backend.resolve_speed = lambda speed: 1.0 if ignore_speed else speed
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        router = TTSRouter(
            [BackendSpec(id="drama3", kind="fake", display="drama3", options={})],
            env={},
            state_path=Path(tmp.name) / "tts_state.json",
            builder=lambda spec: backend,
        )

        if mode is not None:
            router.set_streaming("drama3", mode == "stream")

        player = make_player()
        player.tts = router
        return player

    async def test_a_router_backed_player_streams(self):
        player = self.make()

        self.assertTrue(player._can_stream(speech()))
        stream = await player._open_stream(speech())

        self.assertIsNotNone(stream)
        self.assertEqual(stream.head, b"head")

    async def test_wav_mode_turns_streaming_off(self):
        self.assertFalse(self.make(mode="wav")._can_stream(speech()))

    async def test_speaker_speed_is_ignored_when_the_backend_ignores_it(self):
        self.assertTrue(self.make(ignore_speed=True)._can_stream(speech(speed=1.3)))
        self.assertFalse(self.make()._can_stream(speech(speed=1.3)))


class OpenStreamTests(unittest.IsolatedAsyncioTestCase):
    def make(self, backend):
        player = make_player()
        player.tts = backend
        return player

    async def test_head_is_taken_and_the_rest_stays_lazy(self):
        backend = FakeStreamBackend([b"head", b"tail"])
        player = self.make(backend)

        stream = await player._open_stream(speech())

        self.assertIsNotNone(stream)
        self.assertEqual(stream.head, b"head")

    async def test_a_backend_error_falls_back_to_wav(self):
        backend = FakeStreamBackend([], error=TTSBackendError("ストリーミング非対応"))
        player = self.make(backend)

        self.assertIsNone(await player._open_stream(speech()))

    async def test_no_chunks_falls_back_to_wav(self):
        player = self.make(FakeStreamBackend([]))

        self.assertIsNone(await player._open_stream(speech()))

    async def test_the_backend_sample_rate_is_carried(self):
        # Irodori は 48kHz、DRAMA3 は 44.1kHz。鳴らす側はストリームごとの値を使う。
        backend = FakeStreamBackend([b"head"])
        backend.stream_rate = 48000
        player = self.make(backend)

        stream = await player._open_stream(speech())

        self.assertIsNotNone(stream)
        self.assertEqual(stream.rate, 48000)

    async def test_a_backend_without_a_rate_uses_the_default(self):
        player = self.make(FakeStreamBackend([b"head"]))

        stream = await player._open_stream(speech())

        self.assertIsNotNone(stream)
        self.assertEqual(stream.rate, STREAM_RATE)


class PumpTests(unittest.IsolatedAsyncioTestCase):
    async def read_pipe(self, read_fd: int) -> bytes:
        loop = asyncio.get_running_loop()

        def read_all():
            with os.fdopen(read_fd, "rb") as handle:
                return handle.read()

        return await loop.run_in_executor(None, read_all)

    async def test_head_and_chunks_arrive_in_order(self):
        player = make_player()
        read_fd, write_fd = os.pipe()
        stream = StreamingAudio(speech(), FakeStreamBackend([b"bb", b"cc"]).stream("", ""), b"aa")

        await player._pump_stream(stream, write_fd)

        self.assertEqual(await self.read_pipe(read_fd), b"aabbcc")

    async def test_a_broken_stream_keeps_what_already_arrived(self):
        player = make_player()
        read_fd, write_fd = os.pipe()
        chunks = FakeStreamBackend([b"bb"], error=TTSBackendError("途中で切れました"))
        stream = StreamingAudio(speech(), chunks.stream("", ""), b"aa")

        # 例外は握りつぶし、書き終えた分だけ流す（鳴っている音を止めない）
        await player._pump_stream(stream, write_fd)

        self.assertEqual(await self.read_pipe(read_fd), b"aabb")

    async def test_an_unusable_stream_stops_playback_and_is_marked(self):
        """鳴らしながら「先頭が無音」と分かったら、止めて wav で作り直す印を付ける。"""

        player = make_player()
        stops = []
        player.voice_client = SimpleNamespace(
            is_connected=lambda: True, stop_playing=lambda: stops.append(1)
        )
        read_fd, write_fd = os.pipe()
        chunks = FakeStreamBackend([b"bb"], error=TTSContentError("先頭が無音です"))
        stream = StreamingAudio(speech(), chunks.stream("", ""), b"aa")

        await player._pump_stream(stream, write_fd)

        self.assertTrue(stream.unusable)
        self.assertEqual(stops, [1])
        self.assertEqual(await self.read_pipe(read_fd), b"aabb")

    async def test_a_broken_stream_is_not_marked_unusable(self):
        player = make_player()
        read_fd, write_fd = os.pipe()
        chunks = FakeStreamBackend([b"bb"], error=TTSBackendError("途中で切れました"))
        stream = StreamingAudio(speech(), chunks.stream("", ""), b"aa")

        await player._pump_stream(stream, write_fd)
        await self.read_pipe(read_fd)

        self.assertFalse(stream.unusable)


class DuckVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_keeps_the_item_gain_under_ducking(self):
        player = make_player()
        player.current_source = SimpleNamespace(volume=1.0)
        player.current_gain = 0.2

        player.set_duck(0.5)

        self.assertAlmostEqual(player.current_source.volume, 0.1)

    async def test_wav_playback_is_unchanged(self):
        player = make_player()
        player.current_source = SimpleNamespace(volume=1.0)
        player.current_gain = 1.0

        player.set_duck(0.3)

        self.assertAlmostEqual(player.current_source.volume, 0.3)


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stream_that_cannot_start_is_read_as_wav(self):
        player = make_player()
        player.tts = FakeStreamBackend([b"aa"])
        called = []

        async def fake_play_tts(item):
            called.append(item)

        player._play_tts = fake_play_tts

        async def fake_play_stream(stream):
            return False

        player._play_stream = fake_play_stream
        item = speech()

        player.audio_queue.put_nowait("dummy")

        await player._play_streamed_chunk(
            SimpleNamespace(
                stream=StreamingAudio(item, FakeStreamBackend([]).stream("", ""), b"aa")
            )
        )

        self.assertEqual(called, [item])
        self.assertEqual(player.audio_queue._unfinished_tasks, 0)

    async def test_a_playing_stream_finishes_without_fallback(self):
        player = make_player()
        player.tts = FakeStreamBackend([b"aa"])
        called = []

        async def fake_play_tts(item):
            called.append(item)

        player._play_tts = fake_play_tts

        async def fake_play_stream(stream):
            return True

        player._play_stream = fake_play_stream

        player.audio_queue.put_nowait("dummy")

        await player._play_streamed_chunk(
            SimpleNamespace(
                stream=StreamingAudio(speech(), FakeStreamBackend([]).stream("", ""), b"aa")
            )
        )

        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
