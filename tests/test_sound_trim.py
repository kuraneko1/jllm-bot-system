"""辞書 wav の登録時トリムが、発話の頭と尻を削らないこと。

過去の実装は ``silenceremove`` の ``start_duration`` に 0.08 / 0.12 を渡しており、
その値ぶんだけ**非無音を捨てていた**（合計 200ms）。短い効果音では体感で
「最後まで鳴らない」状態になっていたため、その回帰を防ぐ。
"""

import array
import math
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from jllm_plugins.dictionaries.dictionary import transcode_to_wav

RATE = 48000
THRESHOLD = 300

LEAD_MS = 500
TONE_MS = 1500
TAIL_MS = 600


def build_probe(path: Path) -> None:
    """無音 → 220Hz の音 → 無音、という検出しやすい波形。"""
    frames = array.array("h")
    frames.extend([0] * int(RATE * LEAD_MS / 1000))
    length = int(RATE * TONE_MS / 1000)

    for index in range(length):
        frames.append(int(12000 * math.sin(2 * math.pi * 220 * index / RATE)))

    frames.extend([0] * int(RATE * TAIL_MS / 1000))

    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(frames.tobytes())


def bounds(path: Path):
    """(鳴り始め, 鳴り終わり, 全長) をサンプル数で返す。"""
    with wave.open(str(path)) as reader:
        frames = reader.getnframes()
        params = reader.getparams()
        samples = array.array("h")
        samples.frombytes(reader.readframes(frames))

    first = next((i for i, value in enumerate(samples) if abs(value) > THRESHOLD), None)
    last = next(
        (i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) > THRESHOLD),
        None,
    )
    return first, last, len(samples), params


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class SoundTrimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "probe.wav"
        self.target = self.root / "out.wav"
        build_probe(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def _transcode(self):
        ok = transcode_to_wav(self.source.read_bytes(), ".wav", self.target)
        self.assertTrue(ok, "変換に失敗した")
        return bounds(self.target)

    def test_speech_is_not_shortened(self):
        first, last, _, _ = self._transcode()
        spoken_ms = (last - first) / RATE * 1000
        self.assertGreaterEqual(
            spoken_ms,
            TONE_MS - 20,
            f"発話が削られている（{spoken_ms:.0f}ms / 元 {TONE_MS}ms）",
        )

    def test_leading_silence_is_trimmed(self):
        first, _, _, _ = self._transcode()
        self.assertLess(
            first / RATE * 1000,
            250,
            "先頭の無音が残りすぎている",
        )

    def test_tail_is_not_cut_short(self):
        _, last, total, _ = self._transcode()
        silence_ms = (total - last) / RATE * 1000
        self.assertGreater(
            silence_ms,
            40,
            f"末尾の余白が足りない（{silence_ms:.0f}ms）",
        )

    def test_output_is_mono_48k_s16(self):
        _, _, _, params = self._transcode()
        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.framerate, RATE)
        self.assertEqual(params.sampwidth, 2)


if __name__ == "__main__":
    unittest.main()
