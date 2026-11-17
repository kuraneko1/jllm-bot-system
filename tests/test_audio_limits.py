import io
import math
import struct
import unittest
import wave

from jllm_plugins.playback.player import SpeechItem, SpeechPlayer


def make_wav(seconds, rate=24000, frequency=220.0):
    frames = b"".join(
        struct.pack("<h", int(0.4 * 32767 * math.sin(2 * math.pi * frequency * i / rate)))
        for i in range(int(rate * seconds))
    )
    output = io.BytesIO()

    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)

    return output.getvalue()


class AudioLimitTests(unittest.TestCase):
    def player(self):
        player = object.__new__(SpeechPlayer)
        player._limits = {}
        return player

    def item(self, seconds, group=None):
        return SpeechItem(
            text="", speaker_id=1, speed=1.0, seconds=seconds, limit_group=group
        )

    def test_trims_to_the_requested_seconds(self):
        player = self.player()
        audio = player._limit_audio(self.item(0.5), make_wav(2.0))
        self.assertAlmostEqual(player._wav_seconds(audio), 0.5, places=3)

    def test_short_audio_keeps_its_full_length(self):
        player = self.player()
        audio = player._limit_audio(self.item(3.0), make_wav(0.4))
        self.assertAlmostEqual(player._wav_seconds(audio), 0.4, places=3)

    def test_group_budget_is_shared_across_items(self):
        player = self.player()
        first = player._limit_audio(self.item(1.0, "g"), make_wav(0.6))
        second = player._limit_audio(self.item(1.0, "g"), make_wav(2.0))
        third = player._limit_audio(self.item(1.0, "g"), make_wav(1.0))

        self.assertAlmostEqual(player._wav_seconds(first), 0.6, places=3)
        self.assertAlmostEqual(player._wav_seconds(second), 0.4, places=3)
        self.assertIsNone(third)

    def test_no_limit_returns_the_audio_untouched(self):
        player = self.player()
        audio = make_wav(1.0)
        self.assertIs(player._limit_audio(self.item(None), audio), audio)


if __name__ == "__main__":
    unittest.main()
