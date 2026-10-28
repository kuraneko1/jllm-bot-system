# ruff: noqa: E501
"""nuko（外部ホストの drama3-small）を候補に足したときの配線。

nuko は ``https://nuko.tail822f52.ts.net`` の OpenAI 互換サーバ（``kind: openai``）。
Irodori の話者は持っていないので、**話者一覧は必ずサーバの ``/v1/audio/voices``
から取る**（``tts-backends.json`` に話者名を書かない）。ここではその約束と、
一覧が取れないときの落とし方（サーバの先頭話者／``health`` は ``/health``）を見る。
"""

import json
import unittest
from pathlib import Path

from jllm_plugins.tts_openai.backend import OpenAITTSBackend
from jllm_plugins.tts_registry.registry import build_backend
from jllm_plugins.tts_router.router import load_specs

#: 管理元（このリポジトリ）と、配信後の bot ルート（x230）の両方を見る
BACKENDS_JSON_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "ops" / "x230" / "tts-backends.json",
    Path(__file__).resolve().parents[1] / "tts-backends.json",
]

#: nuko の ``/v1/audio/voices`` が実際に返す形（2026-10-20 実測・先頭だけ）
NUKO_VOICES = ["Clone GOAT", "Clone GOAT2", "Clone Ryu", "Hikari", "野口ユリ"]


class FakeResponse:
    def __init__(self, payload=b"", status: int = 200, content_type: str = "application/json"):
        self._payload = payload
        self.status = status
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._payload

    async def text(self) -> str:
        return self._payload.decode("utf-8", "replace")


class _Context:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """URL ごとの応答を返すだけの aiohttp 代役。"""

    def __init__(self, routes: dict[str, FakeResponse] | None = None):
        self.routes = routes or {}
        self.gets: list[str] = []

    def get(self, url):
        self.gets.append(url)

        return _Context(self.routes.get(url, FakeResponse(b"", 404)))

    async def close(self) -> None:
        pass


def spec_of(ident: str):
    path = next((p for p in BACKENDS_JSON_CANDIDATES if p.is_file()), None)

    if path is None:
        raise unittest.SkipTest("tts-backends.json が見つからない")

    specs = {spec.id: spec for spec in load_specs(path)["specs"]}

    if ident not in specs:
        raise unittest.SkipTest(f"{ident} が tts-backends.json に無い")

    return specs[ident]


class ShippedSpecTests(unittest.TestCase):
    def test_nuko_is_an_openai_backend_over_https(self):
        spec = spec_of("nuko")

        self.assertEqual(spec.kind, "openai")
        self.assertEqual(spec.options["url"], "https://nuko.tail822f52.ts.net")
        self.assertEqual(spec.options["model"], "drama3-small")
        self.assertIn("nuko", spec.label())
        self.assertIn("Tailscale", spec.label())

    def test_the_speaker_list_is_not_hardcoded(self):
        """話者一覧はサーバから取る（JSON に並べない）。

        ``voice`` は一覧が取れないときの控え（サーバにある名前を1つだけ）。
        一覧そのものは ``/v1/audio/voices`` が返す32人を使う。
        """

        spec = spec_of("nuko")

        self.assertFalse(spec.options.get("voices"))
        self.assertIsInstance(spec.options.get("voice"), str)

    def test_speed_is_left_at_one(self):
        """nuko（drama3-small）は ``speed`` を見ないので atempo を掛けない。"""

        spec = spec_of("nuko")

        self.assertEqual(spec.options["default_speed"], 1.0)
        self.assertTrue(spec.options["ignore_speaker_speed"])
        self.assertEqual(spec.options["seed"], 1234)


class BuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_is_built_in_production(self):
        spec = spec_of("nuko")
        env = spec.env_for({})

        self.assertEqual(env["TTS_OPENAI_URL"], "https://nuko.tail822f52.ts.net")
        self.assertEqual(env["TTS_OPENAI_MODEL"], "drama3-small")
        self.assertEqual(env["TTS_OPENAI_SEED"], "1234")

        backend = build_backend(spec.kind, env=env)
        self.addAsyncCleanup(backend.session.close)

        self.assertIsInstance(backend, OpenAITTSBackend)
        self.assertEqual(backend.base_url, "https://nuko.tail822f52.ts.net")
        self.assertEqual(backend.model_name, "drama3-small")
        self.assertEqual(backend.seed, 1234)
        # 一覧が取れないときだけ使う控え（話者一覧そのものはサーバから）
        self.assertEqual(backend.default_voice, "Clone Ryu")

    async def test_health_uses_the_health_endpoint(self):
        spec = spec_of("nuko")
        backend = build_backend(spec.kind, env=spec.env_for({}))
        self.addAsyncCleanup(backend.session.close)

        self.assertEqual(backend.health_path, "/health")
        self.assertEqual(backend.voices_path, "/v1/audio/voices")


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, *, routes=None):
        spec = spec_of("nuko")
        backend = build_backend(spec.kind, env=spec.env_for({}))
        # 本物のセッションは使わない（閉じてから差し替える）
        await backend.session.close()
        backend.session = FakeSession(routes)

        return backend

    async def test_voices_come_from_the_server(self):
        backend = await self.make(routes={
            "https://nuko.tail822f52.ts.net/v1/audio/voices": FakeResponse(
                json.dumps({"data": NUKO_VOICES}).encode(),
            ),
        })

        voices = await backend.list_voices()

        self.assertEqual([voice.name for voice in voices], NUKO_VOICES)
        self.assertIn("https://nuko.tail822f52.ts.net/v1/audio/voices", backend.session.gets)

    async def test_an_unknown_speaker_falls_back_to_the_default_voice(self):
        backend = await self.make(routes={
            "https://nuko.tail822f52.ts.net/v1/audio/voices": FakeResponse(
                json.dumps({"data": NUKO_VOICES}).encode(),
            ),
        })
        await backend.list_voices()

        # Irodori の話者名がそのまま来ても、nuko にある名前へ落とす
        self.assertEqual(backend._resolve_voice("そら"), "Clone Ryu")
        self.assertEqual(backend._resolve_voice("Clone GOAT"), "Clone GOAT")
        self.assertEqual(backend._resolve_voice(1), "Clone GOAT")

    async def test_health_hits_slash_health(self):
        backend = await self.make(routes={
            "https://nuko.tail822f52.ts.net/health": FakeResponse(b'{"status":"ok"}'),
        })

        await backend.health()

        self.assertEqual(backend.session.gets, ["https://nuko.tail822f52.ts.net/health"])


if __name__ == "__main__":
    unittest.main()
