"""音量カーブ（区間ごとの音量点）※実験機能。"""

import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.ducking.duck_curve import DuckCurve, Point, normalize_points


class TargetTests(unittest.TestCase):
    def make(self, points, **kwargs):
        return DuckCurve([Point(*item) for item in points], **kwargs)

    def test_segments_hold_their_volume(self):
        curve = self.make([(0.0, 1.0), (1.0, 0.35), (3.0, 0.2), (8.0, 0.1)])

        self.assertEqual(curve.target(0.0), 1.0)
        self.assertEqual(curve.target(0.9), 1.0)
        self.assertEqual(curve.target(1.0), 0.35)
        self.assertEqual(curve.target(2.9), 0.35)
        self.assertEqual(curve.target(3.0), 0.2)
        self.assertEqual(curve.target(99.0), 0.1)

    def test_reaction_is_the_first_change(self):
        self.assertEqual(self.make([(0.0, 1.0), (1.5, 0.2)]).reaction_seconds, 1.5)
        self.assertEqual(self.make([(0.0, 1.0), (1.0, 1.0)]).reaction_seconds, 0.0)

    def test_level_is_the_lowest_point(self):
        self.assertEqual(self.make([(0.0, 1.0), (1.0, 0.3), (4.0, 0.1)]).level, 0.1)


class NormalizeTests(unittest.TestCase):
    def test_sorted_and_clamped(self):
        points = normalize_points([Point(5, 5), Point(-1, -1), Point(2, 0.5)])

        self.assertEqual([point.seconds for point in points], [0.0, 2.0, 5.0])
        self.assertEqual([point.volume for point in points], [0.0, 0.5, 1.0])

    def test_same_seconds_are_merged_and_count_is_capped(self):
        points = normalize_points(
            [Point(1, 0.5), Point(1, 0.7)] + [Point(index + 2, 0.1) for index in range(20)]
        )

        self.assertLessEqual(len(points), 8)
        self.assertEqual([point for point in points if point.seconds == 1.0][0].volume, 0.7)

    def test_empty_falls_back_to_default(self):
        self.assertEqual([point.volume for point in normalize_points([])], [1.0, 0.2])


class StorageTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duck_curve.json"
            curve = DuckCurve(
                [Point(0, 1.0), Point(1.0, 0.4), Point(4.0, 0.2)],
                attack=0.3,
                release=0.08,
                hold_seconds=1.0,
            )
            self.assertEqual(curve.save(path), path)

            loaded = DuckCurve.load(path)

            self.assertEqual(
                [point.to_dict() for point in loaded.points],
                [point.to_dict() for point in curve.points],
            )
            self.assertAlmostEqual(loaded.attack, 0.3)
            self.assertAlmostEqual(loaded.hold_seconds, 1.0)

    def test_missing_file_uses_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fallback = DuckCurve.from_config(
                trigger_seconds=2.0, level=0.25, attack=0.25, release=0.06, hold_seconds=0.8
            )
            loaded = DuckCurve.load(Path(tmp) / "none.json", fallback=fallback)

            self.assertAlmostEqual(loaded.reaction_seconds, 2.0)
            self.assertAlmostEqual(loaded.level, 0.25)

    def test_broken_file_uses_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duck_curve.json"
            path.write_text("{ not json", encoding="utf-8")

            loaded = DuckCurve.load(path, fallback=DuckCurve.default())

            self.assertEqual(loaded.level, 0.2)

    def test_from_dict_ignores_junk(self):
        curve = DuckCurve.from_dict({"points": [{"seconds": "x"}, {"seconds": 1, "volume": 0.5}]})

        self.assertEqual(len(curve.points), 1)
        self.assertEqual(curve.points[0].volume, 0.5)

    def test_dict_round_trip(self):
        curve = DuckCurve([Point(0, 1.0), Point(2, 0.3)])
        again = DuckCurve.from_dict(json.loads(json.dumps(curve.to_dict())))

        self.assertEqual(again.to_dict()["points"], curve.to_dict()["points"])


if __name__ == "__main__":
    unittest.main()
