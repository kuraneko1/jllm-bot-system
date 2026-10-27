import unittest

from jllm_plugins.speech_text import _apply_trailing_speed, _parse_pieces, _parse_speed_pieces


class InlineSpeedTests(unittest.TestCase):
    def test_token_applies_to_the_whole_preceding_chunk(self):
        # トークンは「直前の区間」全体に掛かる。間に他のトークンが無ければ
        # 区間＝ここまでの本文すべてになる。
        self.assertEqual(
            _parse_speed_pieces("おはよう みんな f2", 1.2),
            [("おはよう みんな", 2.0)],
        )
        self.assertEqual(
            _parse_speed_pieces("A B C f2", 1.2),
            [("A B C", 2.0)],
        )

    def test_groups_scope_each_part_separately(self):
        # 前半 ec3 後半 lo2 → それぞれ直前の部分だけ。
        self.assertEqual(
            _parse_pieces("前半 ec3 後半 lo2", 1.2),
            [("前半", 1.2, (("ec", 3.0),)), ("後半", 1.2, (("lo", 2.0),))],
        )

    def test_effects_stack_in_order(self):
        # 文末に並べると、その順に重ねて掛かる。
        self.assertEqual(
            _parse_pieces("A B ec3 bs20", 1.2),
            [("A B", 1.2, (("ec", 3.0), ("bs", 20.0)))],
        )

    def test_nested_group_matches_flat_form(self):
        # ((本文)ec3)bs20 と (本文)ec3 bs20 は同義。
        flat = _parse_pieces("(A B)ec3 bs20", 1.2)
        nested = _parse_pieces("((A B)ec3)bs20", 1.2)
        self.assertEqual(nested, flat)
        self.assertEqual(flat, [("A B", 1.2, (("ec", 3.0), ("bs", 20.0)))])

    def test_nested_group_applies_inner_then_outer(self):
        # 内側の効果は中身に、外側の効果は全体に掛かる。
        self.assertEqual(
            _parse_pieces("((A)ec3 (B)lo2)bs20", 1.2),
            [("A", 1.2, (("ec", 3.0), ("bs", 20.0))), ("B", 1.2, (("lo", 2.0), ("bs", 20.0)))],
        )

    def test_legacy_echo_is_not_confused_with_ec(self):
        # 正規表現は ecl を ec より先に評価する。ecl5 が ec + l5 に割れないこと。
        self.assertEqual(
            _parse_pieces("A ecl5", 1.2),
            [("A", 1.2, (("ecl", 5.0),))],
        )
        self.assertEqual(
            _parse_pieces("A ec5", 1.2),
            [("A", 1.2, (("ec", 5.0),))],
        )

    def test_all_effect_codes_are_recognized(self):
        from jllm_plugins.audio_effects import EFFECT_CODES

        for code in EFFECT_CODES:
            pieces = _parse_pieces(f"A {code}3", 1.2)
            self.assertEqual(pieces, [("A", 1.2, ((code, 3.0),))], msg=code)

    def test_unclosed_group_is_left_as_text(self):
        self.assertEqual(
            _parse_pieces("(A ec3", 1.2),
            [("(A", 1.2, (("ec", 3.0),))],
        )

    def test_group_applies_to_everything_inside(self):
        self.assertEqual(
            _parse_speed_pieces("(おはよう みんな)f2", 1.2),
            [("おはよう みんな", 2.0)],
        )

    def test_effect_applies_to_the_whole_preceding_chunk(self):
        self.assertEqual(
            _parse_pieces("おはよう みんな rb3", 1.2),
            [("おはよう みんな", 1.2, (("rb", 3.0),))],
        )

    def test_group_is_the_way_to_scope_one_word(self):
        self.assertEqual(
            _parse_pieces("おはよう (みんな)rb3 元気", 1.2),
            [("おはよう", 1.2, ()), ("みんな", 1.2, (("rb", 3.0),)), ("元気", 1.2, ())],
        )

    def test_trailing_command_speed_applies_to_whole_chunk(self):
        pieces = _parse_pieces("おはよう みんな", 1.2)
        self.assertEqual(
            _apply_trailing_speed(pieces, 2.0),
            [("おはよう みんな", 2.0, ())],
        )

    def test_pieces_take_speed_from_preceding_token(self):
        self.assertEqual(
            _parse_speed_pieces("A f2 B f4", 1.2),
            [("A", 2.0), ("B", 4.0)],
        )

    def test_tail_without_token_uses_default(self):
        self.assertEqual(
            _parse_speed_pieces("A f2 B", 1.2),
            [("A", 2.0), ("B", 1.2)],
        )

    def test_no_tokens_is_single_default_piece(self):
        self.assertEqual(
            _parse_speed_pieces("おはようございます", 1.2),
            [("おはようございます", 1.2)],
        )

    def test_bare_f_means_1_5(self):
        self.assertEqual(
            _parse_speed_pieces("A f B", 1.2),
            [("A", 1.5), ("B", 1.2)],
        )

    def test_plain_numbers_are_not_speeds(self):
        self.assertEqual(
            _parse_speed_pieces("12時30分に集合", 1.2),
            [("12時30分に集合", 1.2)],
        )

    def test_full_width_spaces_work(self):
        self.assertEqual(
            _parse_speed_pieces("A　f2　B", 1.2),
            [("A", 2.0), ("B", 1.2)],
        )

    def test_orphan_leading_token_is_dropped(self):
        self.assertEqual(
            _parse_speed_pieces("f2", 1.2),
            [],
        )

    def test_speed_is_clamped(self):
        pieces = _parse_speed_pieces("A f999 B", 1.2)
        self.assertEqual(pieces[0][1], 100.0)
        self.assertEqual(pieces[1], ("B", 1.2))


class InlineEffectTests(unittest.TestCase):
    def test_effect_attaches_to_preceding_chunk(self):
        self.assertEqual(
            _parse_pieces("こんにちは rb", 1.2),
            [("こんにちは", 1.2, (("rb", 1.0),))],
        )

    def test_effect_intensity_parsed_and_clamped(self):
        self.assertEqual(
            _parse_pieces("A rb10 B", 1.2),
            [("A", 1.2, (("rb", 10.0),)), ("B", 1.2, ())],
        )
        self.assertEqual(_parse_pieces("A rb999 B", 1.2)[0][2], (("rb", 20.0),))

    def test_multiple_effects_accumulate(self):
        self.assertEqual(
            _parse_pieces("A rb ch3 B", 1.2),
            [("A", 1.2, (("rb", 1.0), ("ch", 3.0))), ("B", 1.2, ())],
        )

    def test_effects_combine_with_speed_tokens(self):
        self.assertEqual(
            _parse_pieces("A f2 rb B", 1.2),
            [("A", 2.0, ()), ("B", 1.2, (("rb", 1.0),))],
        )
        self.assertEqual(
            _parse_pieces("A rb f2 B", 1.2),
            [("A", 1.2, (("rb", 1.0),)), ("B", 1.2, ())],
        )

    def test_leading_effect_applies_forward(self):
        self.assertEqual(
            _parse_pieces("rb B", 1.2),
            [("B", 1.2, (("rb", 1.0),))],
        )

    def test_trailing_orphan_effect_joins_last_piece(self):
        self.assertEqual(
            _parse_pieces("A f2 rb", 1.2),
            [("A", 2.0, (("rb", 1.0),))],
        )

    def test_effect_only_message_falls_back_to_plain(self):
        self.assertEqual(_parse_pieces("rb", 1.2), [])

    def test_effect_codes_are_case_insensitive(self):
        self.assertEqual(_parse_pieces("A RB10 B", 1.2)[0][2], (("rb", 10.0),))

    def test_plain_words_are_not_effects(self):
        self.assertEqual(
            _parse_pieces("hello world", 1.2),
            [("hello world", 1.2, ())],
        )


class GroupSpeedTests(unittest.TestCase):
    def test_group_applies_speed_to_all_inner_pieces(self):
        self.assertEqual(
            _parse_pieces("(A rb B rb2)f2", 1.2),
            [("A", 2.0, (("rb", 1.0),)), ("B", 2.0, (("rb", 2.0),))],
        )

    def test_group_keeps_inner_speeds_by_default(self):
        self.assertEqual(
            _parse_pieces("X (A f3 B)f2 Y", 1.2),
            [("X", 1.2, ()), ("A", 2.0, ()), ("B", 2.0, ()), ("Y", 1.2, ())],
        )

    def test_bare_group_speed_means_1_5(self):
        self.assertEqual(
            _parse_pieces("(A)f", 1.2),
            [("A", 1.5, ())],
        )

    def test_group_without_suffix_is_literal(self):
        self.assertEqual(
            _parse_pieces("(A)", 1.2),
            [("(A)", 1.2, ())],
        )

    def test_group_effect_applies_to_all_inner_pieces(self):
        self.assertEqual(
            _parse_pieces("(A rb B)ec3", 1.2),
            [("A", 1.2, (("rb", 1.0), ("ec", 3.0))), ("B", 1.2, (("ec", 3.0),))],
        )

    def test_group_bare_effect_means_intensity_1(self):
        self.assertEqual(
            _parse_pieces("(A B)rb", 1.2),
            [("A B", 1.2, (("rb", 1.0),))],
        )

    def test_pending_effect_reaches_into_group(self):
        self.assertEqual(
            _parse_pieces("rb (A)f2", 1.2),
            [("A", 2.0, (("rb", 1.0),))],
        )

    def test_nested_parentheses_are_supported(self):
        # 入れ子の括弧は内側から順に適用される。中身にトークンが無い
        # (A) はそのまま本文として残り、外側の f2 がそれに掛かる。
        self.assertEqual(
            _parse_pieces("((A))f2", 1.2),
            [("(A)", 2.0, ())],
        )

    def test_zero_speed_matches_trailing_command_fallback(self):
        self.assertEqual(
            _parse_pieces("A f0 B", 1.2),
            [("A", 1.5, ()), ("B", 1.2, ())],
        )

    def test_adjacent_groups_without_space(self):
        self.assertEqual(
            _parse_pieces("(A)f2(B)f3", 1.2),
            [("A", 2.0, ()), ("B", 3.0, ())],
        )

    def test_full_width_parentheses_work(self):
        self.assertEqual(
            _parse_pieces("（A）f2", 1.2),
            [("A", 2.0, ())],
        )
        self.assertEqual(
            _parse_pieces("（A rb）ec3", 1.2),
            [("A", 1.2, (("rb", 1.0), ("ec", 3.0)))],
        )

    def test_english_words_with_parens_untouched(self):
        self.assertEqual(
            _parse_pieces("(a)fter party", 1.2),
            [("(a)fter party", 1.2, ())],
        )
        self.assertEqual(
            _parse_pieces("verb(B)f2", 1.2),
            [("verb", 1.2, ()), ("B", 2.0, ())],
        )


if __name__ == "__main__":
    unittest.main()
