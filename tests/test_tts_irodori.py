"""Irodori の SSE ストリーミング（``stream_format=sse``）の検証。

公式 Irodori-TTS-Server は ``stream_format: "sse"`` を付けると、テキストチャンクを
1つ合成し終えるたびに ``audio_chunk`` イベント（base64 の音声）を逐次返す。
ここでは aiohttp を差し替えて、**届いた順に生PCMが取り出せること**と、
**ストリーミング非対応のサーバに当たったら諦めて wav 経路へ戻すこと**を確かめる。
"""

import asyncio
import base64
import json
import unittest

from jllm_plugins.tts_irodori.backend import IrodoriBackend
from jllm_tts import TTSBackendError


def chunk_event(
    pcm: bytes,
    media: str = "audio/pcm",
    fmt: str = "pcm",
) -> tuple[str, dict]:
    return (
        "audio_chunk",
        {
            "index": 0,
            "format": fmt,
            "media_type": media,
            "audio_base64": base64.b64encode(pcm).decode(),
        },
    )


def sse(*events: tuple[str, dict]) -> list[bytes]:
    """SSE の本文を、aiohttp が返すような行の列に組み立てる。"""

    lines: list[bytes] = []

    for name, payload in events:
        lines.append(f"event: {name}\n".encode())
        lines.append(f"data: {json.dumps(payload)}\n".encode())
        lines.append(b"\n")

    return lines


class FakeContent:
    def __init__(self, lines: list[bytes]):
        self.lines = lines

    async def iter_any(self):
        """aiohttp の ``iter_any()`` と同じく、届いた順にそのまま返す。"""

        for line in self.lines:
            yield line


class FakeResponse:
    def __init__(
        self,
        lines: list[bytes] | None = None,
        *,
        status: int = 200,
        content_type: str = "text/event-stream",
        body: str = "",
    ):
        self.status = status
        self.content_type = content_type
        self.content = FakeContent(lines or [])
        self._body = body

    async def text(self) -> str:
        return self._body


class _Context:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """``post`` を記録して、決め打ちの応答を返す aiohttp の代役。"""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.posts: list[dict] = []

    def post(self, url: str, **kwargs) -> _Context:
        self.posts.append({"url": url, **kwargs})
        return _Context(self.response)

    def get(self, url: str, **kwargs) -> _Context:
        return _Context(FakeResponse(status=404, content_type="application/json"))

    async def close(self) -> None:
        pass


def build(response: FakeResponse, env: dict[str, str] | None = None):
    options = env or {}
    streaming = str(options.get("TTS_IRODORI_STREAM", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        first_chunk = int(options.get("TTS_IRODORI_FIRST_CHUNK_MIN_CHARS", "12"))
    except ValueError:
        first_chunk = 12
    backend = IrodoriBackend(
        "http://127.0.0.1:8088",
        streaming=streaming,
        first_chunk_min_chars=first_chunk,
    )
    backend._voices = ["none", "wakamoto"]
    backend.disabled_voices = set()
    session = FakeSession(response)
    original = backend.session
    backend.session = session
    asyncio.get_running_loop().create_task(original.close())
    return backend, session


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_arrive_in_order(self):
        backend, session = build(
            FakeResponse(
                sse(
                    chunk_event(b"\x01\x02"),
                    chunk_event(b"\x03\x04"),
                    ("done", {"chunks": 2}),
                )
            )
        )
        got = [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

        self.assertEqual(got, [b"\x01\x02", b"\x03\x04"])
        # 生PCM（48kHz）で頼んでいること。
        payload = session.posts[0]["json"]
        self.assertEqual(payload["stream_format"], "sse")
        self.assertEqual(payload["response_format"], "pcm")
        self.assertEqual(payload["voice"], "wakamoto")
        self.assertTrue(payload["irodori"]["chunking_enabled"])
        self.assertEqual(session.posts[0]["headers"].get("Accept"), "text/event-stream")

    async def test_first_chunk_can_be_tuned(self):
        backend, session = build(
            FakeResponse(sse(chunk_event(b"\x00\x01"))),
            env={"TTS_IRODORI_FIRST_CHUNK_MIN_CHARS": "15"},
        )
        [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

        options = session.posts[0]["json"]["irodori"]
        self.assertEqual(options["first_sentence_chunk_min_chars"], 15)
        self.assertEqual(options["chunk_min_chars"], 80)

    async def test_a_non_sse_response_falls_back(self):
        backend, _ = build(FakeResponse([b"RIFF...."], content_type="audio/wav"))

        with self.assertRaises(TTSBackendError):
            [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

    async def test_a_wav_chunk_falls_back(self):
        # response_format=pcm を無視して wav を返すサーバ。生PCMとして鳴らすと
        # ノイズになるので、鳴らさずに wav 経路へ戻す。
        backend, _ = build(
            FakeResponse(sse(chunk_event(b"RIFF....", media="audio/wav", fmt="wav")))
        )

        with self.assertRaises(TTSBackendError):
            [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

    async def test_a_server_error_falls_back(self):
        backend, _ = build(FakeResponse(status=500, body="boom"))

        with self.assertRaises(TTSBackendError):
            [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

    async def test_the_error_event_falls_back(self):
        backend, _ = build(FakeResponse(sse(("error", {"detail": "ended before producing audio"}))))

        with self.assertRaises(TTSBackendError):
            [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

    async def test_a_large_chunk_is_not_cut_off(self):
        # base64 が 512KiB を超えるチャンク（数秒ぶんの音声）でも読めること。
        # 行単位で読むと aiohttp の行長上限（LineTooLong）に当たる。
        big = b"\x01\x02" * 300_000
        backend, _ = build(FakeResponse(sse(chunk_event(big))))

        got = [chunk async for chunk in backend.stream("こんにちは。", "wakamoto")]

        self.assertEqual(got, [big])

    async def test_empty_text_yields_nothing(self):
        backend, session = build(FakeResponse(sse(chunk_event(b"\x01"))))

        self.assertEqual([chunk async for chunk in backend.stream("   ", "wakamoto")], [])
        self.assertEqual(session.posts, [])


class EnvTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_can_be_turned_off(self):
        backend, _ = build(FakeResponse(sse()), env={"TTS_IRODORI_STREAM": "0"})

        self.assertFalse(backend.supports_streaming)

    async def test_streaming_is_on_by_default(self):
        backend, _ = build(FakeResponse(sse()))

        self.assertTrue(backend.supports_streaming)
        self.assertEqual(backend.stream_rate, 48000)

    async def test_a_broken_number_falls_back_to_the_default(self):
        backend, _ = build(FakeResponse(sse()), env={"TTS_IRODORI_FIRST_CHUNK_MIN_CHARS": "abc"})

        self.assertEqual(backend.first_chunk_min_chars, 12)
