"""OpenAI 互換バックエンド（services/tts/openai.py）と設定の配線。

drama3-small（``/v1/audio/voices`` が ``{"data": ["test"]}`` を返す）を想定。
"""

import asyncio
import io
import json
import shutil
import unittest
import wave
from pathlib import Path
from unittest import mock

from jllm_plugins.tts_openai.backend import (
    SILENCE_RETRY_SEEDS,
    OpenAITTSBackend,
    TTSSilentStreamError,
    _voice_names,
)
from jllm_tts import TTSBackendError, TTSContentError

#: 管理元（開発機の ops/x230）と、配信後のbotルート（x230）の両方を見る
BACKENDS_JSON_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "ops" / "x230" / "tts-backends.json",
    Path(__file__).resolve().parents[1] / "tts-backends.json",
]


def wav_bytes(frames: int = 44100, rate: int = 44100) -> bytes:
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x10" * frames)

    return buffer.getvalue()


def silent_wav_bytes(frames: int = 44100, rate: int = 44100) -> bytes:
    """DRAMA3 の無音バグを模した、振幅ゼロの wav。"""

    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)

    return buffer.getvalue()


def frames_of(audio: bytes) -> int:
    with wave.open(io.BytesIO(audio)) as reader:
        return reader.getnframes()


class FakeContent:
    """``response.content.iter_chunked(n)`` の代役。"""

    def __init__(self, stream: list[bytes]):
        self.stream = stream

    async def iter_chunked(self, size: int):
        for chunk in self.stream:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        payload: bytes = b"",
        status: int = 200,
        text: str = "",
        content_type: str = "application/json",
        stream: list[bytes] | None = None,
    ):
        self._payload = payload
        self.status = status
        self._text = text
        self.content_type = content_type
        self.content = FakeContent(stream if stream is not None else ([payload] if payload else []))

    async def read(self) -> bytes:
        return self._payload

    async def text(self) -> str:
        return self._text


class _Context:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """``get`` / ``post`` を記録するだけの aiohttp 代役。"""

    def __init__(
        self,
        *,
        payload: bytes = b"",
        routes: dict | None = None,
        post_status: int = 200,
        post_text: str = "",
        payloads: list[bytes] | None = None,
        post_content_type: str = "application/json",
        stream: list[bytes] | None = None,
    ):
        self.payload = payload
        self.routes = routes or {}
        self.post_status = post_status
        self.post_text = post_text
        #: 複数回の合成で違う音声を返したいとき（無音→正常の再試行など）
        self.payloads = payloads
        self.post_content_type = post_content_type
        #: ストリーミング（pcm）で届くチャンク列
        self.stream = stream
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.closed = False

    def get(self, url, **kwargs):
        self.gets.append(url)

        for path, route in self.routes.items():
            if url.endswith(path):
                status, body = route

                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()

                return _Context(FakeResponse(body, status=status))

        return _Context(FakeResponse(b"{}", status=404))

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json", {})))

        if self.payloads:
            payload = self.payloads[min(len(self.posts) - 1, len(self.payloads) - 1)]
        else:
            payload = self.payload

        return _Context(
            FakeResponse(
                payload,
                status=self.post_status,
                text=self.post_text,
                content_type=self.post_content_type,
                stream=self.stream,
            )
        )

    async def close(self) -> None:
        self.closed = True


VOICES_OK = {"/v1/audio/voices": (200, {"object": "list", "data": ["test"]})}
HEALTH_OK = {"/health": (200, {"status": "ok"}), **VOICES_OK}


def attach(backend: OpenAITTSBackend, session: FakeSession) -> OpenAITTSBackend:
    original = backend.session
    backend.session = session
    asyncio.get_running_loop().create_task(original.close())
    return backend


class VoiceNameTests(unittest.TestCase):
    def test_flat_strings(self):
        self.assertEqual(_voice_names({"data": ["test", "foo", "test"]}), ["test", "foo"])

    def test_openai_style_objects(self):
        self.assertEqual(
            _voice_names({"data": [{"id": "alloy"}, {"name": "nova"}]}),
            ["alloy", "nova"],
        )

    def test_bare_list_and_junk(self):
        self.assertEqual(_voice_names(["a", 1, None]), ["a"])
        self.assertEqual(_voice_names(None), [])
        self.assertEqual(_voice_names({"data": "test"}), [])


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voices_come_from_the_voices_endpoint(self):
        backend = attach(
            OpenAITTSBackend("http://192.168.0.22:8090"), FakeSession(routes=VOICES_OK)
        )
        voices = await backend.list_voices()

        self.assertEqual([voice.id for voice in voices], ["test"])
        self.assertEqual([voice.name for voice in voices], ["test"])
        self.assertIn("http://192.168.0.22:8090/v1/audio/voices", backend.session.gets)

    async def test_configured_voices_win_over_the_api(self):
        session = FakeSession(routes=VOICES_OK)
        backend = attach(OpenAITTSBackend("http://127.0.0.1:8090", voices="a, b"), session)

        voices = await backend.list_voices()

        self.assertEqual([voice.name for voice in voices], ["a", "b"])
        self.assertEqual(session.gets, [])

    async def test_default_voice_is_the_last_resort(self):
        backend = attach(
            OpenAITTSBackend("http://127.0.0.1:8090", default_voice="test"), FakeSession()
        )  # 一覧は404

        voices = await backend.list_voices()

        self.assertEqual([voice.name for voice in voices], ["test"])

    async def test_no_voice_at_all_is_empty(self):
        backend = attach(OpenAITTSBackend("http://127.0.0.1:8090"), FakeSession())

        self.assertEqual(await backend.list_voices(), [])

    async def test_resolution_by_name_index_and_default(self):
        backend = attach(
            OpenAITTSBackend("http://127.0.0.1:8090", default_voice="test"),
            FakeSession(routes=VOICES_OK),
        )
        await backend.list_voices()

        self.assertEqual(backend._resolve_voice("test"), "test")
        self.assertEqual(backend._resolve_voice(1), "test")
        self.assertEqual(backend._resolve_voice("知らない人"), "test")
        self.assertEqual(backend._resolve_voice(None), "test")


class AtempoTests(unittest.TestCase):
    def test_in_range_is_one_filter(self):
        self.assertEqual(OpenAITTSBackend._atempo_filter(1.5), "atempo=1.500000")

    def test_above_two_is_split(self):
        self.assertEqual(OpenAITTSBackend._atempo_filter(3.0), "atempo=2.0,atempo=1.500000")

    def test_below_half_is_split(self):
        self.assertEqual(OpenAITTSBackend._atempo_filter(0.4), "atempo=0.5,atempo=0.800000")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が無い")
    def test_apply_tempo_changes_the_length(self):
        source = wav_bytes(frames=44100)
        faster = OpenAITTSBackend._apply_tempo(source, 2.0)
        slower = OpenAITTSBackend._apply_tempo(source, 0.5)

        self.assertIsNotNone(faster)
        self.assertIsNotNone(slower)
        self.assertAlmostEqual(frames_of(faster) / 44100, 0.5, delta=0.1)
        self.assertAlmostEqual(frames_of(slower) / 44100, 2.0, delta=0.1)

    def test_apply_tempo_reports_failure(self):
        with mock.patch(
            "jllm_plugins.tts_openai.backend.subprocess.run", side_effect=OSError("no ffmpeg")
        ):
            self.assertIsNone(OpenAITTSBackend._apply_tempo(b"RIFF", 1.5))


class SynthesizeTests(unittest.IsolatedAsyncioTestCase):
    def make(self, payload: bytes = b"RIFFfake", **kwargs):
        session = FakeSession(payload=payload, routes=HEALTH_OK, **kwargs)
        backend = attach(
            OpenAITTSBackend(
                "http://192.168.0.22:8090", default_voice="test", model_name="drama3-small"
            ),
            session,
        )

        return backend, session

    async def test_payload_has_voice_model_and_format_but_no_speed(self):
        backend, session = self.make()

        audio = await backend.synthesize("こんにちは。", "test", 1.0)

        url, payload = session.posts[0]
        self.assertEqual(url, "http://192.168.0.22:8090/v1/audio/speech")
        self.assertEqual(payload["input"], "こんにちは。")
        self.assertEqual(payload["voice"], "test")
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(payload["model"], "drama3-small")
        # drama3 は speed を見ない（未知フィールドは無視）。既定では送らない
        self.assertNotIn("speed", payload)
        self.assertEqual(audio, b"RIFFfake")

    async def test_server_speed_mode_sends_speed(self):
        session = FakeSession(payload=b"x", routes=HEALTH_OK)
        backend = attach(
            OpenAITTSBackend("http://127.0.0.1:8090", default_voice="test", speed_mode="server"),
            session,
        )

        await backend.synthesize("テスト", "test", 1.5)

        self.assertEqual(session.posts[0][1]["speed"], 1.5)

    async def test_model_is_omitted_when_not_configured(self):
        session = FakeSession(payload=b"x", routes=HEALTH_OK)
        backend = attach(OpenAITTSBackend("http://127.0.0.1:8090", default_voice="test"), session)

        await backend.synthesize("テスト", "test")

        self.assertNotIn("model", session.posts[0][1])

    async def test_atempo_is_used_when_the_speed_differs(self):
        backend, session = self.make()
        backend._apply_tempo = mock.Mock(return_value=b"FAST")

        audio = await backend.synthesize("テスト", "test", 1.5)

        backend._apply_tempo.assert_called_once_with(b"RIFFfake", 1.5)
        self.assertEqual(audio, b"FAST")

    async def test_atempo_failure_falls_back_to_normal_speed(self):
        backend, _ = self.make()
        backend._apply_tempo = mock.Mock(return_value=None)

        self.assertEqual(await backend.synthesize("テスト", "test", 1.5), b"RIFFfake")

    async def test_no_atempo_at_normal_speed(self):
        backend, _ = self.make()
        backend._apply_tempo = mock.Mock(return_value=b"FAST")

        await backend.synthesize("テスト", "test", 1.0)

        backend._apply_tempo.assert_not_called()

    async def test_empty_text_makes_no_request(self):
        backend, session = self.make()

        self.assertEqual(await backend.synthesize("   ", "test"), b"")
        self.assertEqual(session.posts, [])

    async def test_server_error_becomes_a_backend_error(self):
        backend, session = self.make(
            post_status=400, post_text='{"error":{"message":"unknown voice"}}'
        )

        with self.assertRaises(TTSBackendError):
            await backend.synthesize("テスト", "test")


def pcm_bytes(frames: int, amplitude: int = 8000) -> bytes:
    """s16le モノラルの生 PCM（``amplitude`` = 0 なら無音）。"""

    return int(amplitude).to_bytes(2, "little", signed=True) * frames


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    """``response_format=pcm`` ＋ ``streaming=true`` の経路。"""

    def make(self, **kwargs):
        backend_kwargs = {}

        for name in ("streaming",):
            if name in kwargs:
                backend_kwargs[name] = kwargs.pop(name)

        kwargs.setdefault("stream", [pcm_bytes(88200)])
        kwargs.setdefault("post_content_type", "audio/pcm")
        session = FakeSession(routes=HEALTH_OK, **kwargs)
        backend = attach(
            OpenAITTSBackend(
                "http://192.168.0.22:8090",
                default_voice="test",
                model_name="drama3-small",
                **backend_kwargs,
            ),
            session,
        )

        return backend, session

    async def collect(self, backend, text="こんにちは。"):
        chunks = []

        async for chunk in backend.stream(text, "test"):
            chunks.append(chunk)

        return chunks

    async def test_requests_pcm_streaming_and_yields_the_chunks(self):
        backend, session = self.make(stream=[pcm_bytes(88200), pcm_bytes(1000)])

        chunks = await self.collect(backend)

        _, payload = session.posts[0]
        self.assertEqual(payload["response_format"], "pcm")
        self.assertIs(payload["streaming"], True)
        self.assertEqual(payload["voice"], "test")
        # 受け取った順のまま（貯め直さない）
        self.assertEqual(chunks, [pcm_bytes(88200), pcm_bytes(1000)])

    async def test_small_first_block_plays_alone(self):
        """最初のブロックを 16 フレーム（≒ 0.74 秒）に切ったときも、それだけで鳴らし始める。"""

        backend, _ = self.make(stream=[pcm_bytes(16 * 2048), pcm_bytes(21 * 2048)])

        chunks = await self.collect(backend)

        self.assertEqual(chunks, [pcm_bytes(16 * 2048), pcm_bytes(21 * 2048)])

    async def test_first_drama3_block_plays_without_waiting_for_the_second(self):
        """drama3-stream の 1 ブロック（43 フレーム = 88,064 サンプル ≒ 2.0 秒）だけで鳴らし始める。

        判定の閾値が 1 ブロックより大きいと、2 ブロック目（約 1.4 秒後）まで待ってしまう。
        """

        block = 43 * 2048
        backend, _ = self.make(stream=[pcm_bytes(block), pcm_bytes(block)])

        chunks = await self.collect(backend)

        self.assertEqual(chunks, [pcm_bytes(block), pcm_bytes(block)])

    async def test_empty_text_makes_no_request(self):
        backend, session = self.make()

        self.assertEqual(await self.collect(backend, "   "), [])
        self.assertEqual(session.posts, [])

    async def test_a_wav_response_is_refused(self):
        """pcm を知らないサーバに当たったら、生 PCM として鳴らさず wav 経路へ戻す。"""

        backend, _ = self.make(post_content_type="audio/wav")

        with self.assertRaises(TTSBackendError):
            await self.collect(backend)

    async def test_a_silent_head_is_refused_while_playing(self):
        """先頭 1.5 秒が無音なら、届いた分は返したうえで途中で止める（貯めて待たない）。"""

        backend, _ = self.make(stream=[pcm_bytes(4 * 2048, amplitude=0)] * 10 + [pcm_bytes(1000)])
        chunks = []

        with self.assertRaises(TTSSilentStreamError):
            async for chunk in backend.stream("こんにちは。", "test"):
                chunks.append(chunk)

        # 1.5 秒（66,150 サンプル）に届く 9 個目で判定。それより前は鳴らしに回っている
        self.assertEqual(len(chunks), 8)

    async def test_tiny_first_block_is_returned_immediately(self):
        """4 フレーム（≒ 0.19 秒）の最初のブロックでも、待たずにそのまま返す。"""

        backend, _ = self.make(stream=[pcm_bytes(4 * 2048), pcm_bytes(5 * 2048)])

        self.assertEqual(await self.collect(backend), [pcm_bytes(4 * 2048), pcm_bytes(5 * 2048)])

    async def test_a_short_loud_chunk_is_kept(self):
        """2 秒に満たない相づちでも、鳴らせるなら返す。"""

        backend, _ = self.make(stream=[pcm_bytes(1000)])

        self.assertEqual(await self.collect(backend), [pcm_bytes(1000)])

    async def test_server_error_becomes_a_backend_error(self):
        backend, _ = self.make(post_status=502, post_text="RuntimeError: boom", stream=[])

        with self.assertRaises(TTSBackendError):
            await self.collect(backend)

    async def test_streaming_can_be_turned_off(self):
        backend, _ = self.make(streaming=False)

        self.assertFalse(backend.supports_streaming)

    async def test_env_flag_turns_streaming_off(self):
        disabled = OpenAITTSBackend("", streaming=False)
        enabled = OpenAITTSBackend("")
        self.addAsyncCleanup(disabled.session.close)
        self.addAsyncCleanup(enabled.session.close)
        self.assertFalse(disabled.supports_streaming)
        self.assertTrue(enabled.supports_streaming)


class EmptyTakeTests(unittest.IsolatedAsyncioTestCase):
    """DRAMA3 の「音声が 1 つも出なかった」は seed を変えて作り直す。"""

    def make(self, **kwargs):
        session = FakeSession(routes=HEALTH_OK, **kwargs)
        backend = attach(
            OpenAITTSBackend(
                "http://192.168.0.22:8090", default_voice="test", seed=1234, silence_retries=1
            ),
            session,
        )

        return backend, session

    async def test_empty_take_is_retried_with_another_seed(self):
        backend, session = self.make(
            post_status=502,
            post_text='{"error":{"message":"RuntimeError: drama3-small ended before '
            'producing audio; try another seed"}}',
        )
        # 2 回目は成功させる（payloads は status を変えられないので _speak を差し替える）
        calls = []

        async def fake_speak(text, voice, speed, seed=None):
            calls.append(seed)

            if len(calls) == 1:
                raise TTSBackendError(
                    "音声合成に失敗しました: 502 RuntimeError: drama3-small ended "
                    "before producing audio; try another seed"
                )

            return wav_bytes(frames=1000)

        backend._speak = fake_speak

        audio = await backend.synthesize("テスト", "test")

        self.assertEqual(audio, wav_bytes(frames=1000))
        self.assertEqual(calls, [1234, 42], "別の seed で作り直していません")

    async def test_other_errors_are_not_retried(self):
        backend, _ = self.make(post_status=400, post_text='{"error":{"message":"unknown voice"}}')
        calls = []

        async def fake_speak(text, voice, speed, seed=None):
            calls.append(seed)
            raise TTSBackendError("音声合成に失敗しました: 400 unknown voice")

        backend._speak = fake_speak

        with self.assertRaises(TTSBackendError):
            await backend.synthesize("テスト", "test")

        self.assertEqual(calls, [1234], "関係ないエラーまで再試行しています")

    async def test_exhausted_empty_takes_are_a_content_error(self):
        # seed を使い切っても空なら「この 1 件だけ」の失敗（ルーターが切り替えない目印）
        backend, _ = self.make()

        async def fake_speak(text, voice, speed, seed=None):
            raise TTSBackendError(
                "音声合成に失敗しました: 502 drama3-small ended before producing audio"
            )

        backend._speak = fake_speak

        with self.assertRaises(TTSContentError):
            await backend.synthesize("テスト", "test")


class SilenceDetectionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        backend = OpenAITTSBackend("http://127.0.0.1:8090", **kwargs)
        self.addAsyncCleanup(backend.session.close)

        return backend

    async def test_loud_wav_is_not_silent(self):
        backend = self.make()
        decibels = backend._rms_dbfs(wav_bytes(frames=1000))

        self.assertIsNotNone(decibels)
        self.assertGreater(decibels, -30)
        self.assertIsNone(backend._is_silent(wav_bytes(frames=1000)))

    async def test_zero_amplitude_wav_is_silent(self):
        backend = self.make()
        silent = silent_wav_bytes(frames=1000)

        self.assertEqual(backend._rms_dbfs(silent), -999.0)
        self.assertEqual(backend._is_silent(silent), -999.0)

    async def test_threshold_is_configurable(self):
        backend = self.make(silence_rms_dbfs=-5.0)

        # -18 dBFS は既定の -45 なら正常だが、-5 なら「無音」扱い
        self.assertIsNotNone(backend._is_silent(wav_bytes(frames=1000)))

    async def test_unreadable_audio_is_not_judged(self):
        backend = self.make()

        self.assertIsNone(backend._rms_dbfs(b"RIFFnotawav"))
        self.assertIsNone(backend._rms_dbfs(b""))
        self.assertIsNone(backend._is_silent(b""))


class SpeedPolicyTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        session = FakeSession(payload=b"RIFFfake", routes=HEALTH_OK)
        backend = attach(
            OpenAITTSBackend(
                "http://127.0.0.1:8090", default_voice="test", model_name="drama3-small", **kwargs
            ),
            session,
        )

        return backend, session

    async def test_default_is_one_to_one(self):
        backend, _ = self.make()

        self.assertEqual(backend.resolve_speed(1.5), 1.5)
        self.assertEqual(backend.resolve_speed(None), 1.0)
        self.assertEqual(backend.resolve_speed(float("nan")), 1.0)
        self.assertEqual(backend.resolve_speed(0.0), 1.0)

    async def test_ignore_speaker_speed_forces_default(self):
        backend, session = self.make(default_speed=1.0, ignore_speaker_speed=True)
        backend._apply_tempo = mock.Mock(return_value=b"FAST")

        # 話者設定 1.5 を渡しても 1.0（=等速）で読み、atempo は呼ばない
        audio = await backend.synthesize("テスト", "test", 1.5)

        self.assertEqual(audio, b"RIFFfake")
        backend._apply_tempo.assert_not_called()
        self.assertNotIn("speed", session.posts[0][1])

    async def test_ignore_speaker_speed_uses_default_atempo(self):
        backend, _ = self.make(default_speed=1.5, ignore_speaker_speed=True)
        backend._apply_tempo = mock.Mock(return_value=b"SLOW")

        audio = await backend.synthesize("テスト", "test", 1.0)

        backend._apply_tempo.assert_called_once_with(b"RIFFfake", 1.5)
        self.assertEqual(audio, b"SLOW")

    async def test_speed_mode_fixed_is_the_alias(self):
        backend, _ = self.make(speed_mode="fixed")
        backend._apply_tempo = mock.Mock(return_value=b"FAST")

        await backend.synthesize("テスト", "test", 2.0)

        self.assertTrue(backend.ignore_speaker_speed)
        self.assertEqual(backend.speed_mode, "atempo")
        backend._apply_tempo.assert_not_called()

    async def test_server_mode_still_receives_the_resolved_speed(self):
        backend, session = self.make(
            speed_mode="server", default_speed=1.0, ignore_speaker_speed=True
        )

        await backend.synthesize("テスト", "test", 1.5)

        self.assertEqual(session.posts[0][1]["speed"], 1.0)


class SeedAndRetryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, payload: bytes = b"RIFFfake", **backend_kwargs):
        session = FakeSession(payload=payload, routes=HEALTH_OK)
        backend = attach(
            OpenAITTSBackend(
                "http://127.0.0.1:8090",
                default_voice="test",
                model_name="drama3-small",
                **backend_kwargs,
            ),
            session,
        )

        return backend, session

    async def test_seed_is_sent_when_configured(self):
        session = FakeSession(payload=wav_bytes(frames=100), routes=HEALTH_OK)
        backend = attach(
            OpenAITTSBackend("http://127.0.0.1:8090", default_voice="test", seed=1234), session
        )

        await backend.synthesize("テスト", "test", 1.0)

        self.assertEqual(session.posts[0][1]["seed"], 1234)

    async def test_no_seed_is_sent_by_default(self):
        backend, session = self.make()

        await backend.synthesize("テスト", "test", 1.0)

        self.assertNotIn("seed", session.posts[0][1])

    async def test_silent_result_is_retried_with_another_seed(self):
        silent, loud = silent_wav_bytes(frames=1000), wav_bytes(frames=1000)
        session = FakeSession(routes=HEALTH_OK, payloads=[silent, loud])
        backend = attach(
            OpenAITTSBackend(
                "http://127.0.0.1:8090", default_voice="test", seed=1, silence_retries=1
            ),
            session,
        )

        audio = await backend.synthesize("テスト", "test", 1.0)

        self.assertEqual(audio, loud)
        self.assertEqual(len(session.posts), 2)
        self.assertEqual(session.posts[0][1]["seed"], 1)
        # 設定seedと重複しない、実測で正常だった値へ切り替える
        self.assertEqual(session.posts[1][1]["seed"], SILENCE_RETRY_SEEDS[0])

    async def test_retries_are_capped(self):
        silent = silent_wav_bytes(frames=1000)
        session = FakeSession(routes=HEALTH_OK, payload=silent)
        backend = attach(
            OpenAITTSBackend(
                "http://127.0.0.1:8090", default_voice="test", seed=1, silence_retries=2
            ),
            session,
        )

        await backend.synthesize("テスト", "test", 1.0)

        self.assertEqual(len(session.posts), 3)
        seeds = [payload["seed"] for _, payload in session.posts]
        self.assertEqual(len(set(seeds)), 3)

    async def test_retry_can_be_disabled(self):
        silent = silent_wav_bytes(frames=1000)
        session = FakeSession(routes=HEALTH_OK, payload=silent)
        backend = attach(
            OpenAITTSBackend(
                "http://127.0.0.1:8090", default_voice="test", seed=1, silence_retries=0
            ),
            session,
        )

        audio = await backend.synthesize("テスト", "test", 1.0)

        self.assertEqual(audio, silent)
        self.assertEqual(len(session.posts), 1)

    async def test_normal_result_is_not_retried(self):
        backend, session = self.make(payload=wav_bytes(frames=1000), seed=1234)

        await backend.synthesize("テスト", "test", 1.0)

        self.assertEqual(len(session.posts), 1)

    async def test_health_hits_the_health_endpoint(self):
        backend = attach(
            OpenAITTSBackend("http://192.168.0.22:8090"), FakeSession(routes=HEALTH_OK)
        )

        await backend.health()

        self.assertEqual(backend.session.gets, ["http://192.168.0.22:8090/health"])

    async def test_health_falls_back_to_the_voices_path(self):
        backend = attach(
            OpenAITTSBackend("http://192.168.0.22:8090"), FakeSession(routes=VOICES_OK)
        )  # /health は404

        await backend.health()

        self.assertEqual(
            backend.session.gets,
            ["http://192.168.0.22:8090/health", "http://192.168.0.22:8090/v1/audio/voices"],
        )

    async def test_health_reports_a_connection_failure(self):
        session = FakeSession(routes=HEALTH_OK)
        session.get = mock.Mock(side_effect=OSError("boom"))
        backend = attach(OpenAITTSBackend("http://192.168.0.22:8090"), session)

        with self.assertRaises(TTSBackendError):
            await backend.health()


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_defaults_match_legacy(self):
        backend = OpenAITTSBackend("")
        self.addAsyncCleanup(backend.session.close)
        self.assertEqual(backend.voices_path, "/v1/audio/voices")
        self.assertEqual(backend.health_path, "/health")
        self.assertEqual(backend.speed_mode, "atempo")
        self.assertEqual(backend.default_speed, 1.0)
        self.assertFalse(backend.ignore_speaker_speed)
        self.assertIsNone(backend.seed)
        self.assertEqual(backend.silence_retries, 1)


if __name__ == "__main__":
    unittest.main()
