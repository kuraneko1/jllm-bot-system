import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary


class SoundDictionaryTests(unittest.TestCase):
    def test_load_split_add_volume_and_remove(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "sounds.json"
            data.write_text(
                json.dumps(
                    {
                        "mar": "a.wav",
                        "すごい": {"files": ["b.wav"], "by": "tester", "volumes": {"b.wav": 0.4}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = SoundDictionary(str(data), str(root / "sounds"))
            segments = store.split("前 mar 後")
            self.assertEqual([segment.kind for segment in segments], ["text", "sound", "text"])
            self.assertTrue(segments[1].value.endswith("a.wav"))
            self.assertEqual(store.split("tomar")[0].kind, "text")
            self.assertEqual(store.volume_of_file("b.wav"), 0.4)
            self.assertEqual(store.author_of("すごい"), "tester")

            store.add("new", ["c.wav", "d.wav"], "author")
            store.set_volumes("new", {"c.wav": 0.7, "d.wav": 1.0, "missing.wav": 0.1})
            reloaded = SoundDictionary(str(data), str(root / "sounds"))
            self.assertEqual(reloaded.volume_of_file("c.wav"), 0.7)
            self.assertEqual(reloaded.volume_of_file("d.wav"), 1.0)
            self.assertEqual(reloaded.author_of("new"), "author")
            self.assertTrue(reloaded.remove("new"))
            self.assertFalse(reloaded.remove("new"))


class ReadingDictionaryTests(unittest.TestCase):
    def test_apply_preserves_text_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "readings.json")
            path.write_text(
                json.dumps(
                    {"カタカナ": {"reading": "かたかな", "by": "tester"}, "v100": "ぶいひゃく"}
                ),
                encoding="utf-8",
            )
            readings = ReadingDictionary(str(path))
            self.assertEqual(readings.apply("V100とカタカナ"), "ぶいひゃくとかたかな")
            self.assertEqual(readings.author_of("カタカナ"), "tester")
            readings.enabled = False
            self.assertEqual(readings.apply("V100"), "V100")
            readings.enabled = True
            readings.add("mar", "まあ", "author")
            self.assertIn("mar", dict(ReadingDictionary(str(path)).items()))
            self.assertTrue(readings.remove("mar"))
            self.assertFalse(readings.remove("mar"))


if __name__ == "__main__":
    unittest.main()
