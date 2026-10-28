# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""ASCII art (AA): detection and drift-free rendering.

Japanese AA ("SJIS art") is composed against MS PGothic's proportional widths,
so it is set in Saitamaar, which reproduces those widths. At multiples of 16px
every advance is a whole pixel, so the block is drawn at such a size and then
scaled as one picture: no per-glyph rounding, no reflow, spaces kept exactly.
Latin ASCII art is composed for monospace and uses JetBrains Mono (ligatures
off, so ``->`` or ``||`` keep their own glyphs).
"""

from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache

import emoji
from PIL import Image, ImageColor, ImageDraw

from .miq_fonts import bundled_fonts, coverage, font_at

FENCE = re.compile(r"\A\s*```[^\n`]*\n(.*?)\n?```\s*\Z", re.S)
# Box drawing, blocks and shades are drawn by monospace fonts on the cell grid.
MONO_EXTRA = re.compile(r"[─-▟]")
# Characters that are typical of SJIS art even without spacing.
SJIS_MARKS = set("´｀∀Дﾟω∧∨＿￣｜／＼（）ヽﾉ⌒・゜ﾟ〃ノ丿ミ彡∩∪⊂⊃")
SPACES = re.compile(r"[ 　]{2,}|^[ 　]+")
MONO_FEATURES = ("-calt", "-liga", "-dlig")


def strip_fence(text):
    """Discord code blocks (```...```) are verbatim by definition."""

    matched = FENCE.match(text)
    return (matched.group(1), True) if matched else (text, False)


def detect(text, fonts=None):
    """Return ``(mode, art)`` for ASCII art, else ``None``.

    ``mode`` is ``"mono"`` (ASCII/box drawing) or ``"sjis"`` (proportional AA).
    ``fonts`` is the role -> path mapping from ``bundled_fonts()``; it is looked
    up lazily so ``detect`` also works on its own.
    """

    body, fenced = strip_fence(text)
    lines = [line.rstrip() for line in body.expandtabs(4).split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    visible = [c for c in "".join(lines) if not c.isspace()]
    if not visible:
        return None
    if not fenced:
        if len(lines) < 2:
            return None
        symbols = sum(1 for c in visible if _symbolic(c))
        spaced = sum(1 for line in lines if SPACES.search(line))
        marks = sum(1 for c in visible if c in SJIS_MARKS)
        ratio = symbols / len(visible)
        if not (ratio >= 0.5 or (ratio >= 0.25 and spaced >= 2) or marks >= 3):
            return None
    # Remove indentation shared by every line; relative alignment is unchanged.
    indent = min(
        (len(line) - len(line.lstrip(" ")) for line in lines if line.strip()),
        default=0,
    )
    lines = [line[indent:] for line in lines]
    mono = all(ord(c) < 128 or MONO_EXTRA.match(c) for c in "".join(lines))
    if not mono:
        # Saitamaar draws U+005C as a yen sign, as MS PGothic did; Discord shows a
        # backslash. U+2216 is a backslash with the same advance.
        lines = [line.replace("\\", "\u2216") for line in lines]
    # Every glyph of the block comes from one face, so a character that face
    # cannot draw disappears without a trace. Emoji, Arabic and CJK outside the
    # AA repertoire belong to the normal text path, which has emoji and fallback
    # faces; only art the AA face can actually draw is treated as art.
    if not _drawable("".join(lines), "mono" if mono else "aa", fonts):
        return None
    return ("mono" if mono else "sjis"), lines


def _drawable(text, role, fonts):
    """True when the single face for ``role`` can draw every visible character."""

    if fonts is None:
        try:
            fonts = bundled_fonts()
        except OSError, ValueError:
            return all(
                ord(char) <= 0xFFFF
                and not emoji.is_emoji(char)
                and not (0x0600 <= ord(char) <= 0x06FF)
                for char in text
            )
    cmap = coverage(fonts[role])
    return all(char.isspace() or ord(char) in cmap for char in text)


def _symbolic(char):
    category = unicodedata.category(char)
    return category[0] in "PS" or char in SJIS_MARKS


@lru_cache(maxsize=64)
def _measure(path, px, line, features):
    return font_at(path, px).getlength(line, features=list(features) or None)


def layout(lines, mode, fonts, width, height, maximum):
    """Choose the pixel size; returns ``(px, render_px, block_w, block_h, spec)``."""

    path = fonts["aa"] if mode == "sjis" else fonts["mono"]
    features = () if mode == "sjis" else MONO_FEATURES
    base = 16 if mode == "sjis" else 20
    leading = 18 / 16 if mode == "sjis" else 1.25
    widest = max((_measure(path, base, line, features) for line in lines), default=1)
    block = (max(1.0, widest), max(1.0, len(lines) * base * leading))
    px = min(maximum, base * width / block[0], base * height / block[1])
    # Render at a whole multiple of the base size (integral advances), then shrink.
    render = base * max(1, math.ceil(px / base))
    return px, render, path, features, leading


def draw(canvas, lines, mode, fonts, box, fill, maximum, align="center", valign="middle"):
    """Paint the art inside ``box``; returns its rectangle."""

    left, top, right, bottom = box
    px, render, path, features, leading = layout(
        lines, mode, fonts, right - left, bottom - top, maximum
    )
    font = font_at(path, render)
    step = render * leading
    width = math.ceil(max(font.getlength(line, features=list(features) or None) for line in lines))
    mask = Image.new("L", (max(1, width + 2), max(1, math.ceil(step * len(lines)) + 2)))
    pen = ImageDraw.Draw(mask)
    ascent = font.getmetrics()[0]
    for index, line in enumerate(lines):
        if line.strip():
            pen.text(
                (0, round(index * step + (step - render) / 2 + ascent)),
                line,
                font=font,
                fill=255,
                anchor="ls",
                features=list(features) or None,
            )
    scale = px / render
    size = (max(1, round(mask.width * scale)), max(1, round(mask.height * scale)))
    if size != mask.size:
        mask = mask.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
    x = {"left": left, "right": right - size[0]}.get(align, (left + right - size[0]) / 2)
    y = {"top": top, "bottom": bottom - size[1]}.get(valign, (top + bottom - size[1]) / 2)
    if isinstance(fill, str):
        fill = ImageColor.getrgb(fill)
    color = tuple(fill[:3]) if canvas.mode == "RGB" else tuple(fill[:3]) + (255,)
    canvas.paste(color, (round(x), round(y)), mask)
    return (x, y, x + size[0], y + size[1])


def banner(text, face, cells=16):
    """Big block letters (██) for the terminal layout's one or two characters."""

    font = font_at(face, cells * 4)
    l, t, r, b = font.getbbox(text, anchor="ls")
    image = Image.new("L", (max(1, r - l), max(1, b - t)))
    ImageDraw.Draw(image).text((-l, -t), text, font=font, fill=255, anchor="ls")
    rows = max(4, cells)
    cols = max(1, round(image.width / image.height * rows))
    small = image.resize((cols, rows), Image.Resampling.BOX)
    data = small.tobytes()
    return [
        "".join("██" if data[y * cols + x] > 110 else "  " for x in range(cols)).rstrip()
        for y in range(rows)
    ]
