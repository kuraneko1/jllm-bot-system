# ruff: noqa: E501
import unittest
from unittest.mock import patch

from jllm_plugins.dashboard.playground import Playground
from jllm_plugins.dictionaries.dictionary import Segment


class _StubTts:
    async def synthesize(self, text, speaker_id, speed):
        return b"RIFF-fake"


class _StubDictionary:
    def split(self, text):
        return [Segment("text", text)]


class _StubReadings:
    def apply(self, text):
        return text


class PlaygroundEffectsTests(unittest.IsolatedAsyncioTestCase):
    async def test_effects_use_the_injected_service(self):
        service = object()
        playground = Playground(_StubTts(), _StubDictionary(), _StubReadings(), effects=service)
        with patch("jllm_plugins.dashboard.playground.SpeechPlayer._apply_effects",
                   side_effect=lambda audio, effects, **kwargs: audio) as apply:
            with patch.object(Playground, "_concat", side_effect=lambda chunks: chunks[0]):
                await playground.synthesize("(こんにちは)ec3", "voice", 1.0)
        apply.assert_called_once()
        self.assertIs(apply.call_args.kwargs["effect_service"], service)

    async def test_without_service_the_default_path_is_used(self):
        playground = Playground(_StubTts(), _StubDictionary(), _StubReadings())
        with patch("jllm_plugins.dashboard.playground.SpeechPlayer._apply_effects",
                   side_effect=lambda audio, effects, **kwargs: audio) as apply:
            with patch.object(Playground, "_concat", side_effect=lambda chunks: chunks[0]):
                await playground.synthesize("(こんにちは)ec3", "voice", 1.0)
        self.assertIsNone(apply.call_args.kwargs["effect_service"])


if __name__ == "__main__":
    unittest.main()
