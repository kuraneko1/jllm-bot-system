import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.wavrank.service import WavRankConfig, boundary_pattern, compute, normalize, render
from PIL import Image


class WavRankComputeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "messages.jsonl"
        self.sounds = self.root / "sounds.json"
        self.sounds.write_text(
            json.dumps({"mar": {}, "すごい": {}, "カタカナ": {}}, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_records(self, records):
        self.archive.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_normalize_and_ascii_boundary(self):
        self.assertEqual(normalize("ＭＡＲ カタカナ"), "mar かたかな")
        self.assertNotEqual(boundary_pattern("mar"), boundary_pattern("すごい"))

    def test_compute_matches_old_counting_rules(self):
        self.write_records(
            [
                {
                    "author": {"id": 42, "global_name": "先の名前", "username": "user"},
                    "timestamp": "2026-01-01T00:00:00Z",
                    "content": "tomar",
                },
                {
                    "author": {"id": "42", "global_name": "後の名前", "username": "user"},
                    "timestamp": "2026-01-02T00:00:00Z",
                    "content": "mar mar すごい",
                },
                {
                    "author": {"id": 42, "username": "user"},
                    "timestamp": "2026-01-03T00:00:00Z",
                    "content": "カタカナとすごい",
                },
                {
                    "author": {"id": 42, "username": "user"},
                    "timestamp": "2026-01-04T00:00:00Z",
                    "content": "",
                },
                {
                    "author": {"id": 99, "global_name": "別の人"},
                    "timestamp": "2026-01-05T00:00:00Z",
                    "content": "mar",
                },
            ]
        )
        stats = compute(42, str(self.archive), str(self.sounds))
        self.assertEqual(stats["user"], "42")
        self.assertEqual(stats["msgs"], 4)
        self.assertEqual(stats["fired"], 2)
        self.assertEqual(stats["unique"], 3)
        self.assertEqual(dict(stats["rows"]), {"mar": 2, "すごい": 2, "カタカナ": 1})
        self.assertEqual(stats["name"], "user")
        self.assertEqual(stats["first"], "2026-01-01")
        self.assertEqual(stats["last"], "2026-01-04")

    def test_empty_and_missing_inputs(self):
        self.write_records(
            [
                {"author": {"id": 42, "username": "user"}, "content": "関係ない本文"},
            ]
        )
        self.assertEqual(compute(42, str(self.archive), str(self.sounds))["rows"], [])
        with self.assertRaises(FileNotFoundError):
            compute(42, str(self.root / "missing.jsonl"), str(self.sounds))
        with self.assertRaises(FileNotFoundError):
            compute(42, str(self.archive), str(self.root / "missing.json"))
        with self.assertRaises(ValueError):
            compute(42, sounds=str(self.sounds))

    def test_render_writes_png_to_configured_output(self):
        self.write_records(
            [
                {"author": {"id": 42, "username": "user"}, "content": "mar"},
            ]
        )
        config = WavRankConfig(data_dir=self.root)
        stats = compute(42, str(self.archive), str(self.sounds), config=config)
        output = Path(render(stats, scale=1, config=config))
        self.assertEqual(output, self.root / "wav_rank" / "42.png")
        with Image.open(output) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1400, 880))
        self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
