# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Make-it-a-Quote 風の画像生成。

``quote.py`` / ``miq_*.py`` は https://github.com/kuraneko1/miq-generator の
33構図版（PR #5）を土台に、AA対応と bot 側の差分を統合したもの。
生成側の変更は上流に入れてから、こちらへ持ってくる。

同梱のときにだけ入れている差分:

- 兄弟モジュールの import を相対 import に変更（``from miq_x import`` → ``from .miq_x import``）。
- ``miq_fonts.FONT_DIR`` を環境変数 ``MIQ_FONT_DIR`` で差し替え可能に
  （botは ``data/miq_fonts`` を指す。``tools/setup_miq_fonts.py`` で取得）。
- Pango/Cairo が無い環境では、複雑な文字の整形をあきらめて Pillow 側で描く
  （``MixedFont.complex_text()`` が False を返す）。
- アスキーアート（AA）は main の ``miq_aa.py`` を移植。AA用フォント
  （Saitamaar / JetBrains Mono）が描けない文字を含む場合は通常のテキスト経路。

    render(avatar: Path, text: str, output: Path,
           name: str = "", handle: str = "",
           font_file: str | None = None, bold_font_file: str | None = None,
           size=None, *, layout="auto", seed=None, attachment=False) -> None

- 構図は **33種類**（横27 + 縦/正方形6）。``auto`` はその向きの構図から等確率で
  抽選し、続けて構図に合う書体を抽選する。``MIQ_LAYOUT`` に名前を書くと固定、
  ``seed`` で再現もできる。古い ``mirror`` は ``mirrored`` の互換名。
- 掛け合い（``miq_dialogue.py``、vs / love / talk）と構図台帳
  （``miq_catalog.py``）も同梱する。台帳の見本アイコンは、同梱先に
  ``demo-avatar.png`` が無ければ単色タイルで代用する。
- 既定は **640x360**（``MIQ_SIZE=854x480`` / ``1920x1080`` なども可。
  縦は ``MIQ_PORTRAIT_SIZE``、正方形は ``MIQ_SQUARE_SIZE``）
- **縦は伸ばさない**（同じ比率のまま）。入る最大の文字サイズを探し、それでも
  入らないときは本文を縦に縮めて収める（文字が潰れるのは許容。省略はしない）
- 句読点が行頭に来ない・開き括弧が行末に残らない（禁則処理）
- アスキーアート（AA）は検出すると行を折り返さず1ブロックで描く。
- Discord記法（太字・スポイラー・メンション・タイムスタンプ等）を整形する。

依存は Pillow・Pango/Cairo・日本語フォント（フォントは ``MIQ_FONT`` でも指定できる）。
"""

from .quote import (
    AUTO_LAYOUTS,
    BASE_SIZE,
    LAYOUT_WEIGHTS,
    LAYOUTS,
    SIZE,
    SIZES,
    choose_layout,
    compose,
    default_size,
    find_font,
    find_layout,
    render,
    render_bytes,
    resolve_align,
    resolve_size,
    text_region,
    warmup,
)

__all__ = [
    "AUTO_LAYOUTS",
    "BASE_SIZE",
    "LAYOUTS",
    "LAYOUT_WEIGHTS",
    "SIZE",
    "SIZES",
    "choose_layout",
    "compose",
    "default_size",
    "find_layout",
    "find_font",
    "render",
    "render_bytes",
    "resolve_align",
    "resolve_size",
    "text_region",
    "warmup",
]
