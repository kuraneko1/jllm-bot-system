import unittest

from jllm_tts import TTSBackend, TTSBackendError, Voice, VoicevoxError


class DummyBackend(TTSBackend):
    name = "dummy"
    default_speed = 1.2
    ignore_speaker_speed = False

    async def list_voices(self):
        return [Voice("a", "A"), Voice(2, "B")]

    async def synthesize(self, text, speaker_id, speed=1.0):
        return f"{text}:{speaker_id}:{speed}".encode()


class TTSContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_speakers_view_and_voice_id(self):
        backend = DummyBackend()
        self.assertEqual(
            await backend.speakers(),
            [{"name": "dummy", "styles": [{"id": "a", "name": "A"}, {"id": 2, "name": "B"}]}],
        )
        self.assertEqual(await backend.synthesize("hello", "a", 1.5), b"hello:a:1.5")

    async def test_speed_policy_and_alias(self):
        backend = DummyBackend()
        self.assertEqual(backend.resolve_speed(None), 1.2)
        self.assertEqual(backend.resolve_speed(1.8), 1.8)
        self.assertEqual(backend.resolve_speed(float("nan")), 1.2)
        backend.ignore_speaker_speed = True
        self.assertEqual(backend.resolve_speed(2.0), 1.2)
        self.assertIs(VoicevoxError, TTSBackendError)


if __name__ == "__main__":
    unittest.main()
