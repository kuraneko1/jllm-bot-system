import unittest
from unittest.mock import AsyncMock

from jllm_plugins.tts_voicevox.backend import VoicevoxBackend
from jllm_tts import Voice


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status=200):
        self.payload = payload
        self.content = content
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)

    async def json(self):
        return self.payload

    async def read(self):
        return self.content


class FakeSession:
    def __init__(self):
        self.calls = []
        self.close = AsyncMock()

    def get(self, url):
        self.calls.append(("get", url))
        if url.endswith("/speakers"):
            return FakeResponse(
                payload=[
                    {
                        "name": "speaker",
                        "styles": [{"id": 3, "name": "ノーマル"}, {"id": 8, "name": "あまあま"}],
                    }
                ]
            )
        return FakeResponse(payload={"version": "test"})

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/audio_query"):
            return FakeResponse(payload={"text": "hello", "speedScale": 1.0})
        return FakeResponse(content=b"RIFFfake")


class VoicevoxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = VoicevoxBackend("http://example:50021/", default_speaker=8)
        await self.backend.session.close()
        self.session = FakeSession()
        self.backend.session = self.session

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_speaker_resolution_matches_legacy(self):
        self.backend._voices = [Voice(id=3, name="ノーマル"), Voice(id=8, name="あまあま")]
        self.assertEqual(await self.backend._resolve_speaker(3), 3)
        self.assertEqual(await self.backend._resolve_speaker("3"), 3)
        self.assertEqual(await self.backend._resolve_speaker("ノーマル"), 3)
        self.assertEqual(await self.backend._resolve_speaker("clone"), 8)
        self.assertEqual(await self.backend._resolve_speaker(999), 8)
        self.backend.default_speaker = None
        self.assertEqual(await self.backend._resolve_speaker("clone"), 3)

    async def test_http_contract_and_speed(self):
        voices = await self.backend.list_voices()
        self.assertEqual(voices, [Voice(3, "ノーマル"), Voice(8, "あまあま")])
        self.assertEqual(await self.backend.synthesize("hello", "ノーマル", 1.5), b"RIFFfake")
        query = next(call for call in self.session.calls if call[1].endswith("/audio_query"))
        synthesis = next(call for call in self.session.calls if call[1].endswith("/synthesis"))
        self.assertEqual(query[2]["params"]["speaker"], 3)
        self.assertEqual(synthesis[2]["json"]["speedScale"], 1.5)
        await self.backend.health()
        self.assertEqual(self.backend.describe(), "voicevox (http://example:50021)")


if __name__ == "__main__":
    unittest.main()
