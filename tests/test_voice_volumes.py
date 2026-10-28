# ruff: noqa: E501
import tempfile
import unittest

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes
from jllm_tts import Voice


class VoiceVolumesTests(unittest.TestCase):
    def test_default_is_unity_and_invalid_values_rejected(self):
        store = VoiceVolumes()
        self.assertEqual(store.get("nobody"), 1.0)
        for bad in (-0.1, 3.1, float("nan"), float("inf"), "loud", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.set("voice", bad)
        self.assertEqual(store.get("voice"), 1.0)

    def test_boundaries_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/voice_volumes.json"
            store = VoiceVolumes(path)
            self.assertEqual(store.set("a", 0), 0)
            self.assertEqual(store.set("b", 3), 3)
            self.assertEqual(store.set("c", 2.5), 2.5)
            reloaded = VoiceVolumes(path)
            self.assertEqual(reloaded.get("a"), 0)
            self.assertEqual(reloaded.get("b"), 3)
            self.assertEqual(reloaded.get("c"), 2.5)
            self.assertTrue(reloaded.remove("c"))
            self.assertFalse(reloaded.remove("c"))
            self.assertEqual(VoiceVolumes(path).get("c"), 1.0)

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/voice_volumes.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            self.assertEqual(VoiceVolumes(path).get("a"), 1.0)


def make_reader(tts):
    cog = ReaderCog.__new__(ReaderCog)
    cog.tts = tts
    cog.voice_volumes = VoiceVolumes()
    cog.voice_speeds = VoiceSpeeds()
    cog._voice_id_names = {}
    return cog


class FakeTTS:
    def __init__(self, voices=None, fail=False):
        self.voices = voices or [Voice(id=1, name="none"), Voice(id=2, name="Clone Ryu")]
        self.fail = fail

    async def list_voices(self):
        if self.fail:
            raise RuntimeError("tts down")
        return self.voices


class TtsGainTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_lookup(self):
        cog = make_reader(FakeTTS())
        cog.voice_volumes.set("Clone Ryu", 2.5)
        self.assertEqual(await cog.tts_gain_for("Clone Ryu"), 2.5)
        self.assertEqual(await cog.tts_gain_for("none"), 1.0)

    async def test_id_lookup_caches_names(self):
        tts = FakeTTS()
        cog = make_reader(tts)
        cog.voice_volumes.set("Clone Ryu", 2.0)
        self.assertEqual(await cog.tts_gain_for(2), 2.0)
        self.assertEqual(cog._voice_id_names, {1: "none", 2: "Clone Ryu"})
        tts.voices = []
        self.assertEqual(await cog.tts_gain_for(2), 2.0)

    async def test_unknown_id_and_backend_failure_fall_back(self):
        cog = make_reader(FakeTTS())
        self.assertEqual(await cog.tts_gain_for(99), 1.0)
        failing = make_reader(FakeTTS(fail=True))
        self.assertEqual(await failing.tts_gain_for(2), 1.0)
        self.assertEqual(await failing.tts_gain_for(None), 1.0)

if __name__ == "__main__":
    unittest.main()
