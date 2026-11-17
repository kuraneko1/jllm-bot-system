import unittest

from jllm_plugins.speech_text import _parse_pieces, _parse_pieces_ex


class InlineSecondsTests(unittest.TestCase):
    def test_seconds_apply_to_the_preceding_chunk(self):
        self.assertEqual(
            _parse_pieces_ex("A 3s B 6s", 1.2),
            [("A", 1.2, (), 3.0), ("B", 1.2, (), 6.0)],
        )

    def test_decimals_are_allowed(self):
        self.assertEqual(_parse_pieces_ex("A 0.5s", 1.2)[0][3], 0.5)

    def test_comma_separator(self):
        self.assertEqual(
            _parse_pieces_ex("wavテキスト 3s,テキスト 6s", 1.2),
            [("wavテキスト", 1.2, (), 3.0), ("テキスト", 1.2, (), 6.0)],
        )

    def test_seconds_combine_with_effects(self):
        self.assertEqual(
            _parse_pieces_ex("A ec3 3s", 1.2),
            [("A", 1.2, (("ec", 3.0),), 3.0)],
        )

    def test_group_suffix(self):
        self.assertEqual(_parse_pieces_ex("(A B)3s", 1.2), [("A B", 1.2, (), 3.0)])

    def test_legacy_parser_drops_seconds(self):
        self.assertEqual(_parse_pieces("A 3s", 1.2), [("A", 1.2, ())])


if __name__ == "__main__":
    unittest.main()
