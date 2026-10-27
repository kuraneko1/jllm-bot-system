"""speech.cpp の Irodori-TTS（GGUF）バックエンド（services/tts/speechcpp.py）。"""

import io
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from jllm_plugins.tts_speechcpp.backend import SpeechCppBackend
from jllm_tts import TTSBackendError


def wav_bytes(frames: int, rate: int = 48000) -> bytes:
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00" * frames)

    return buffer.getvalue()


class FakeResponse:
    def __init__(self, payload: bytes = b"", status: int = 200, text: str = ""):
        self._payload = payload
        self.status = status
        self._text = text
        self.raise_for_status_called = False

    async def read(self) -> bytes:
        return self._payload

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True

        if self.status >= 400:
            raise AssertionError("raise_for_status should not be reached")


class FakeSession:
    """``post`` / ``get`` を記録するだけの aiohttp 代役。"""

    def __init__(self, payload: bytes = b"", models: dict | None = None):
        self.payload = payload
        self.models = models
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.closed = False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json", {})))

        return _Context(FakeResponse(self.payload))

    def get(self, url, **kwargs):
        self.gets.append(url)

        if url.endswith("/v1/models"):
            body = json.dumps(self.models if self.models is not None else {"data": []})

            return _Context(FakeResponse(body.encode()))

        return _Context(FakeResponse(b'{"status":"ok"}'))

    async def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc) -> bool:
        return False


class SplitTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(SpeechCppBackend.split_text("こんにちは。"), ["こんにちは。"])

    def test_long_text_is_split_within_the_limit(self):
        text = "長い文章です。" * 60
        chunks = SpeechCppBackend.split_text(text, limit=50)

        self.assertGreater(len(chunks), 1)

        for chunk in chunks:
            self.assertLessEqual(len(chunk), 50)

        self.assertEqual("".join(chunks), text)

    def test_blank_text_makes_no_chunk(self):
        self.assertEqual(SpeechCppBackend.split_text("   \n  "), [])

    def test_newlines_are_kept_inside_chunks(self):
        chunks = SpeechCppBackend.split_text("一行目。\n二行目。")

        self.assertEqual("".join(chunks), "一行目。\n二行目。")


class CombineTests(unittest.TestCase):
    def test_frames_add_up(self):
        combined = SpeechCppBackend.combine_wavs([wav_bytes(10), wav_bytes(5)])

        with wave.open(io.BytesIO(combined)) as reader:
            self.assertEqual(reader.getnframes(), 15)
            self.assertEqual(reader.getframerate(), 48000)
            self.assertEqual(reader.getnchannels(), 1)

    def test_single_chunk_is_returned_as_is(self):
        only = wav_bytes(3)

        self.assertIs(SpeechCppBackend.combine_wavs([only]), only)

    def test_nothing_is_empty(self):
        self.assertEqual(SpeechCppBackend.combine_wavs([]), b"")


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

        for name in ("Clone Ryu", "Clone GOAT"):
            (self.directory / f"{name}.voice.gguf").write_bytes(b"x")

    def offline(self, backend: SpeechCppBackend) -> SpeechCppBackend:
        """サーバに繋がない偽セッションを挿す（一覧はディレクトリから）。"""

        session = FakeSession()
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        return backend

    async def test_voices_come_from_the_server(self):
        backend = SpeechCppBackend("http://127.0.0.1:8089", voices_dir=str(self.directory))
        session = FakeSession(
            models={
                "data": [
                    {
                        "speech": {
                            "voices": [{"name": "none"}, {"name": "ryu"}, {"name": "Clone GOAT"}]
                        }
                    }
                ]
            }
        )
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        voices = await backend.list_voices()

        self.assertEqual([voice.name for voice in voices], ["none", "ryu", "Clone GOAT"])
        self.assertIn("http://127.0.0.1:8089/v1/models", session.gets)

    async def test_voices_fall_back_to_the_directory(self):
        backend = SpeechCppBackend("http://127.0.0.1:8089", voices_dir=str(self.directory))
        session = FakeSession()  # /v1/models は空
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        voices = await backend.list_voices()

        # none（参照音声なし）が先頭、あとはファイル名順
        self.assertEqual([voice.name for voice in voices], ["none", "Clone GOAT", "Clone Ryu"])

    async def test_resolution_by_name_index_and_default(self):
        backend = self.offline(
            SpeechCppBackend(
                "http://127.0.0.1:8089", voices_dir=str(self.directory), default_voice="Clone Ryu"
            )
        )
        await backend.list_voices()

        self.assertEqual(backend._resolve_voice("Clone GOAT"), "Clone GOAT")
        self.assertEqual(backend._resolve_voice(2), "Clone GOAT")  # none が1
        self.assertEqual(backend._resolve_voice("知らない人"), "Clone Ryu")
        self.assertEqual(backend._resolve_voice(None), "Clone Ryu")

    async def test_unknown_default_falls_back_to_none(self):
        backend = self.offline(
            SpeechCppBackend(
                "http://127.0.0.1:8089", voices_dir=str(self.directory), default_voice="知らない人"
            )
        )

        self.assertEqual(backend._resolve_voice("知らない人"), "none")


class SynthesizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        (self.directory / "ryu.voice.gguf").write_bytes(b"x")

    def make(self, payload: bytes) -> tuple[SpeechCppBackend, FakeSession]:
        backend = SpeechCppBackend("http://127.0.0.1:8089", voices_dir=str(self.directory))
        session = FakeSession(payload)
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        return backend, session

    async def test_payload_has_no_model_and_keeps_voice_and_speed(self):
        backend, session = self.make(wav_bytes(10))

        audio = await backend.synthesize("テストです。", "ryu", 1.5)

        url, payload = session.posts[0]
        self.assertEqual(url, "http://127.0.0.1:8089/v1/audio/speech")
        self.assertNotIn("model", payload)  # 送ると404になる
        self.assertEqual(payload["voice"], "ryu")
        self.assertEqual(payload["speed"], 1.5)
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(len(audio), len(wav_bytes(10)))

    async def test_long_text_is_split_and_joined(self):
        backend, session = self.make(wav_bytes(10))
        text = "長い文章です。" * 60

        audio = await backend.synthesize(text, "ryu", 1.0)

        self.assertGreater(len(session.posts), 1)
        self.assertEqual("".join(payload["input"] for _, payload in session.posts), text)

        with wave.open(io.BytesIO(audio)) as reader:
            self.assertEqual(reader.getnframes(), 10 * len(session.posts))

    async def test_model_name_is_sent_when_configured(self):
        backend = SpeechCppBackend(
            "http://127.0.0.1:8089",
            voices_dir=str(self.directory),
            model_name="Irodori-TTS-v4.1-Small-MF",
        )
        session = FakeSession(wav_bytes(1))
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        await backend.synthesize("テスト", "ryu")

        self.assertEqual(session.posts[0][1]["model"], "Irodori-TTS-v4.1-Small-MF")

    async def test_server_error_becomes_a_backend_error(self):
        backend, session = self.make(b"")
        session.post = lambda url, **kwargs: _Context(
            FakeResponse(b"", status=404, text='{"error":{"code":"model_not_found"}}')
        )

        with self.assertRaises(TTSBackendError):
            await backend.synthesize("テスト", "ryu")

    async def test_empty_text_makes_no_request(self):
        backend, session = self.make(wav_bytes(1))

        self.assertEqual(await backend.synthesize("   ", "ryu"), b"")
        self.assertEqual(session.posts, [])


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_hits_the_health_endpoint(self):
        backend = SpeechCppBackend("http://127.0.0.1:8089")
        session = FakeSession()
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        await backend.health()

        self.assertEqual(session.gets, ["http://127.0.0.1:8089/health"])

    async def test_health_reports_a_failure(self):
        backend = SpeechCppBackend("http://127.0.0.1:8089")
        session = FakeSession()
        session.get = mock.Mock(side_effect=OSError("boom"))
        self.addAsyncCleanup(backend.session.close)
        backend.session = session
        self.addCleanup(session.close)

        with self.assertRaises(TTSBackendError):
            await backend.health()


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_defaults_and_custom_values(self):
        backend = SpeechCppBackend("http://127.0.0.1:8089")
        self.addAsyncCleanup(backend.session.close)
        self.assertEqual(backend.default_voice, "none")
        custom = SpeechCppBackend(
            "http://example:9999",
            voices_dir="/tmp/voices",
            default_voice="Clone Ryu",
            model_name="irodori-tts",
            timeout=30,
        )
        self.addAsyncCleanup(custom.session.close)
        self.assertEqual(custom.base_url, "http://example:9999")
        self.assertEqual(custom.voices_dir, Path("/tmp/voices"))
        self.assertEqual(custom.default_voice, "Clone Ryu")
        self.assertEqual(custom.model_name, "irodori-tts")


if __name__ == "__main__":
    unittest.main()
