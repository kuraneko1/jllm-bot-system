# ruff: noqa: E501
import unittest

from jllm_plugins.dashboard.playground import Playground
from jllm_plugins.dictionaries.dictionary import Segment


class _StubDictionary:
    def split(self, text):
        return [Segment("text", text)]


class _StubReadings:
    def apply(self, text):
        return text


class PlaygroundPlanTests(unittest.TestCase):
    """実験場は読み上げ本体と同じ規約で区間を組み立てる。"""

    def setUp(self):
        self.playground = Playground(None, _StubDictionary(), _StubReadings())

    def test_trailing_effects_cover_the_whole_text(self):
        self.assertEqual(
            self.playground.plan("前半 後半 ec3 bs20", 1.0),
            [("前半 後半", 1.0, (("ec", 3.0), ("bs", 20.0)))],
        )

    def test_middle_effect_covers_only_the_part_before_it(self):
        self.assertEqual(
            self.playground.plan("前半 ec3 後半", 1.0),
            [("前半", 1.0, (("ec", 3.0),)), ("後半", 1.0, ())],
        )

    def test_group_and_nesting(self):
        flat = self.playground.plan("(前半)ec3 bs20", 1.0)
        nested = self.playground.plan("((前半)ec3)bs20", 1.0)
        self.assertEqual(flat, nested)

    def test_base_speed_is_used_for_untagged_parts(self):
        self.assertEqual(
            self.playground.plan("ふつう", 1.5),
            [("ふつう", 1.5, ())],
        )

    def test_trailing_command_speed_applies_to_everything(self):
        self.assertEqual(
            self.playground.plan("前半 後半 f2", 1.0),
            [("前半 後半", 2.0, ())],
        )


if __name__ == "__main__":
    unittest.main()
