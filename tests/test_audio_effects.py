import math
import tempfile
import unittest
import wave
from pathlib import Path

from jllm_plugins.audio_effects import (
    EFFECT_CODES,
    EFFECTS,
    IR_RATE,
    build_filter,
    clamp_intensity,
    describe_effects,
)

# 畳み込みグラフを組む ec 以外（単純なフィルタ文字列を返すもの）。
LINEAR_CODES = ("rb", "ecl", "ch", "vi", "lo", "bs", "pi", "rv", "lm", "wd", "ph", "ex")


class AudioEffectsTests(unittest.TestCase):
    def test_all_codes_build_filters(self):
        self.assertEqual(
            EFFECT_CODES,
            (
                "rb",
                "ec",
                "ecl",
                "ch",
                "vi",
                "lo",
                "bs",
                "pi",
                "rv",
                "lm",
                "wd",
                "ph",
                "ex",
                "kk",
                "tc",
            ),
        )
        for code in LINEAR_CODES:
            filtrate = build_filter([(code, 1.0)])
            self.assertTrue(filtrate, msg=code)
            self.assertNotIn(" ", filtrate, msg=code)
            self.assertTrue(
                all(part and "=" in part or part == "areverse" for part in filtrate.split(",")),
                msg=code,
            )

    def test_removed_effect_codes_are_gone(self):
        # fl(フランジャー) と tr(トレモロ) は削除。ph は「電話」からフェイザーへ変更。
        for code in ("fl", "tr"):
            self.assertIsNone(build_filter([(code, 5.0)]), msg=code)
            self.assertNotIn(code, EFFECTS, msg=code)

    def test_empty_and_unknown_codes_yield_none(self):
        self.assertIsNone(build_filter([]))
        self.assertIsNone(build_filter(()))
        self.assertIsNone(build_filter([("xx", 1.0)]))

    def test_intensity_clamped(self):
        self.assertEqual(clamp_intensity(0), 1.0)
        self.assertEqual(clamp_intensity(999), 20.0)
        self.assertEqual(clamp_intensity("bad"), 1.0)
        self.assertEqual(clamp_intensity(float("nan")), 1.0)
        self.assertNotEqual(build_filter([("bs", 1.0)]), build_filter([("bs", 10.0)]))
        self.assertIn("alimiter", build_filter([("bs", 10.0)]))

    def test_reverse_ignores_intensity(self):
        self.assertEqual(build_filter([("rv", 1.0)]), "areverse")
        self.assertEqual(build_filter([("rv", 10.0)]), "areverse")

    # ---------------------------------------------------------------- ec
    def test_echo_is_convolution_graph(self):
        filtrate = build_filter([("ec", 5.0)])
        self.assertIn("amovie=", filtrate)
        self.assertIn("afir=", filtrate)
        self.assertIn("asplit=2", filtrate)
        self.assertIn("amix=inputs=2", filtrate)
        self.assertIn(f"aresample={IR_RATE}", filtrate)
        self.assertIn("level=false", filtrate)

    def test_echo_forces_stereo_before_convolution(self):
        # 辞書 wav はモノラル。afir は既定 (irfmt=input) で入力のチャンネル数に
        # 従うため、ステレオ化しないとモノラル出力になり広がりが出ない。
        filtrate = build_filter([("ec", 5.0)])
        stereo = "aformat=channel_layouts=stereo"
        self.assertIn(stereo, filtrate)
        self.assertLess(filtrate.index(stereo), filtrate.index("asplit=2"))

    def test_echo_ir_path_is_configurable(self):
        filtrate = build_filter([("ec", 5.0)], ir_path="custom-room.wav")
        self.assertIn("amovie=custom-room.wav", filtrate)

    def test_echo_uses_nonzero_dry(self):
        # afir の dry は ffmpeg のバージョンで挙動が違う。bot が動く 7.1.3 では
        # dry が小さいと出力が無音になる（0 も 0.001 も無音）。dry=1 を使い、
        # 元音声との混合は build_filter の amix で行う。
        filtrate = build_filter([("ec", 5.0)])
        afir = filtrate.split("afir=")[1].split("[")[0]
        self.assertIn("dry=1:", afir)
        self.assertNotIn("dry=0.001", afir)
        self.assertIn("amix=inputs=2", filtrate)

    def test_tree_chime_synthesizes_metallic_partials(self):
        # ツリーチャイムは sine 源を足し合わせる。金属棒のインハーモニック比。
        filtrate = build_filter([("tc", 10.0)])
        self.assertIn("sine=frequency=", filtrate)
        self.assertIn("afade=t=out", filtrate)
        self.assertIn("adelay=", filtrate)
        self.assertIn("amix=inputs=", filtrate)
        # 基音の 2.76 倍と 5.40 倍が入っていること（金属棒の倍音）
        self.assertIn("3242.06", filtrate)
        self.assertIn("6343.16", filtrate)

    def test_sparkle_is_a_branching_graph(self):
        # kk はピッチの違う枝を並列に重ねるため、フィルタグラフになる。
        filtrate = build_filter([("kk", 5.0)])
        self.assertIn("asplit=4", filtrate)
        self.assertIn("rubberband=pitch=1.4983", filtrate)
        self.assertIn("rubberband=pitch=2.0", filtrate)
        self.assertIn("rubberband=pitch=2.997", filtrate)
        self.assertIn("amix=inputs=4", filtrate)
        self.assertIn("haas=", filtrate)

    def test_graph_effects_take_pre_effects(self):
        # 先行効果は {PRE} に入り、グラフの入力側に置かれる。
        filtrate = build_filter([("pi", 3.0), ("kk", 5.0)])
        self.assertIn("rubberband=pitch=1.1892", filtrate)
        self.assertLess(
            filtrate.index("rubberband=pitch=1.1892"),
            filtrate.index("asplit=4"),
        )

    def test_legacy_echo_keeps_discrete_taps(self):
        # 従来の ec 実装を ecl としてそのまま残している。強さ5 で 450ms/1000ms。
        filtrate = build_filter([("ecl", 5.0)])
        self.assertIn("aecho=", filtrate)
        self.assertIn("450|1000", filtrate)
        # 畳み込み（フィルタグラフ）ではない
        self.assertNotIn("amovie=", filtrate)

    def test_echo_and_legacy_echo_are_distinct(self):
        self.assertNotEqual(build_filter([("ec", 5.0)]), build_filter([("ecl", 5.0)]))

    def test_echo_combines_with_other_effects(self):
        filtrate = build_filter([("rb", 3.0), ("ec", 6.0), ("pi", 2.0)])
        # 他の効果は畳み込みの前に、残響だけが amovie の枝に入る。
        self.assertLess(filtrate.index("aecho"), filtrate.index("amovie="))
        self.assertLess(filtrate.index("rubberband"), filtrate.index("amovie="))
        self.assertLess(filtrate.index("amovie="), filtrate.index("amix=inputs=2"))

    def test_echo_pads_before_convolution_to_keep_the_tail(self):
        # afir は出力を入力長で切って残響の尻尾を捨てる。畳み込む前に
        # 無音を足しておかないと、余韻が消えてエコーに聴こえない。
        with tempfile.TemporaryDirectory() as temp:
            ir_path = Path(temp) / "room.wav"
            with wave.open(str(ir_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(48000)
                handle.writeframes(b"\x00\x00" * 100)
            filtrate = build_filter([("ec", 5.0)], ir_path=str(ir_path))
        self.assertIn("apad=pad_dur=", filtrate)
        self.assertLess(filtrate.index("asplit=2"), filtrate.index("apad=pad_dur="))
        self.assertLess(filtrate.index("apad=pad_dur="), filtrate.index("afir="))
        pad = float(filtrate.split("apad=pad_dur=")[1].split("[")[0])
        self.assertGreater(pad, 0.0)

    def test_echo_wet_grows_with_intensity(self):
        def wet(intensity):
            filtrate = build_filter([("ec", intensity)])
            return float(filtrate.split(",volume=")[1].split("[")[0])

        self.assertLess(wet(1.0), wet(10.0))
        self.assertLess(wet(10.0), wet(20.0))
        # 低強度でも聴こえること。以前は強さ1で相関0.98＝ほぼ素の声で、
        # ec3 が「エコーを感じない」状態になっていた。
        self.assertGreaterEqual(wet(1.0), 20.0)

    # ------------------------------------------------------- 品質の歯止め
    def test_lofi_band_never_inverts(self):
        # lo は旧 ph の帯域制限を統合している。highpass < lowpass を保つ。
        for intensity in (1.0, 5.0, 10.0, 20.0):
            filtrate = build_filter([("lo", intensity)])
            high = int(filtrate.split("highpass=f=")[1].split(",")[0])
            low = int(filtrate.split("lowpass=f=")[1].split(",")[0])
            self.assertLess(high, low, msg=filtrate)
            self.assertGreater(low - high, 200, msg=filtrate)

    def test_lofi_enables_anti_aliasing(self):
        self.assertIn("aa=1", build_filter([("lo", 5.0)]))

    def test_vibrato_rate_is_capped(self):
        # 強さ20 で 43Hz になり「ブルブル」になっていたのを 11Hz で止める。
        rate = float(build_filter([("vi", 20.0)]).split("f=")[1].split(":")[0])
        self.assertLessEqual(rate, 11.0)
        soft = float(build_filter([("vi", 1.0)]).split("f=")[1].split(":")[0])
        self.assertLess(soft, rate)

    def test_bass_gain_is_capped(self):
        # 強さ20 で 43dB になり音量が暴騰していたのを 12dB で止める。
        gain = float(build_filter([("bs", 20.0)]).split("g=")[1].split(":")[0])
        self.assertLessEqual(gain, 12.0)

    def test_loud_uses_compressor_with_linear_threshold(self):
        filtrate = build_filter([("lm", 10.0)])
        self.assertIn("acompressor", filtrate)
        # acompressor の threshold は dB ではなくリニア値（0..1）。
        threshold = float(filtrate.split("threshold=")[1].split(":")[0])
        self.assertGreaterEqual(threshold, 0.02)
        self.assertLessEqual(threshold, 1.0)

    def test_limiter_disables_auto_level(self):
        # alimiter の level は既定 true で、出力を 0dB へ正規化してしまう。
        # クリップ防止のつもりがフルスケールまで持ち上げるため level=false が必須。
        for code in ("lo", "bs", "lm", "wd", "ph", "ec"):
            filtrate = build_filter([(code, 5.0)])
            self.assertIn("alimiter", filtrate, msg=code)
            self.assertIn("level=false", filtrate, msg=code)

    def test_widen_forces_stereo_and_haas(self):
        filtrate = build_filter([("wd", 5.0)])
        self.assertIn("aformat=channel_layouts=stereo", filtrate)
        self.assertIn("haas=", filtrate)

    def test_pitch_uses_semitones(self):
        filtrate = build_filter([("pi", 12.0)])
        factor = float(filtrate.split("=")[-1])
        self.assertAlmostEqual(factor, 2.0, places=3)
        self.assertTrue(math.isfinite(factor))

    def test_combination_keeps_order_and_single_limiter(self):
        filtrate = build_filter([("rb", 2.0), ("pi", 4.0), ("bs", 1.0)])
        self.assertLess(filtrate.index("aecho"), filtrate.index("rubberband"))
        self.assertEqual(filtrate.count("alimiter"), 1)

    def test_describe_uses_japanese_labels(self):
        self.assertEqual(describe_effects([("rb", 10.0)]), "リバーブx10")
        self.assertEqual(describe_effects([("rb", 1.0), ("xx", 2.0)]), "リバーブx1")
        self.assertEqual(describe_effects([]), "")
        for code, (label, _, _) in EFFECTS.items():
            self.assertTrue(label, msg=code)


if __name__ == "__main__":
    unittest.main()
