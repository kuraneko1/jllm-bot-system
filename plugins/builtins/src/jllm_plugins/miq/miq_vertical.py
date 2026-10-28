# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Vertical Japanese (縦書き) through Pango's gravity support.

The layout is shaped in a context rotated by 90°, so Pango lays lines out as
columns running right to left, uses vertical glyph forms (、。「」ー…) and
turns Latin runs sideways, as in printed Japanese.
"""

from __future__ import annotations

import math

import cairocffi as cairo
import pangocairocffi as pangocairo
import pangocffi as pango
import regex

from .miq_complex import BitmapTiles, protect_words, register_font
from .miq_words import CUSTOM, GRAPHEME, runs

GRAVITY_AUTO = 4
HINT_NATURAL = 0
HINT_STRONG = 1
CJK = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}ー々〆〇]")
RTL = regex.compile(r"[\p{Arabic}\p{Hebrew}\p{Syriac}\p{Thaana}]")


def suits_vertical(text, limit=220):
    """Mostly Japanese, short enough to stay legible, nothing that needs bidi."""

    if CUSTOM.search(text) or RTL.search(text):
        return False
    letters = regex.findall(r"[\p{L}\p{N}]", text)
    if not letters or len(GRAPHEME.findall(text)) > limit:
        return False
    return sum(bool(CJK.match(c)) for c in letters) >= 0.5 * len(letters)


SHORT_ASCII = regex.compile(
    r"(?<![A-Za-z0-9.,:/])[0-9]{1,4}(?![A-Za-z0-9.,:/])|(?<![A-Za-z])[!?]{1,2}"
)


def upright(text):
    """Short numbers and !? stand upright as full-width forms (縦中横風).

    Longer Latin runs keep their natural sideways orientation.
    """

    return SHORT_ASCII.sub(lambda m: "".join(chr(ord(c) + 0xFEE0) for c in m.group()), text)


def _layout(ctx, text, family, px, column):
    layout = pangocairo.create_layout(ctx)
    context = pango.pango.pango_layout_get_context(layout.pointer)
    pango.pango.pango_context_set_base_gravity(context, GRAVITY_AUTO)
    pango.pango.pango_context_set_gravity_hint(context, HINT_NATURAL)
    pango.pango.pango_layout_context_changed(layout.pointer)
    desc = pango.FontDescription()
    desc.family = family.label + ", Noto Sans JP, Noto Color Emoji"
    desc.set_absolute_size(pango.units_from_double(px))
    if family.body:
        desc.weight = family.body
    layout.font_description = desc
    layout.wrap = pango.WrapMode.WORD
    layout.text = text
    # Keep Japanese phrases together, exactly like horizontal wrapping.
    attrs = pango.AttrList()
    protect_words(attrs, text)
    # Emoji stand upright like kanji instead of following the Latin rotation.
    offset = 0
    for token, kind in runs(text, True):
        end = offset + len(token.encode())
        if kind == "emoji":
            attr = pango.Attribute.from_pointer(
                pango.pango.pango_attr_gravity_hint_new(HINT_STRONG)
            )
            attr.start_index, attr.end_index = offset, end
            attrs.insert(attr)
        offset = end
    layout.attributes = attrs
    layout.spacing = pango.units_from_double(px * 0.45)
    layout.width = pango.units_from_double(column)
    return layout


def draw_vertical(canvas, scene, region, ink, max_px):
    """Fit the quote into ``region`` as right-to-left columns and paint it."""

    family = scene.family
    register_font(family.path)
    text = upright(scene.text)
    left, top, right, bottom = region
    column = bottom - top
    span = right - left
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    ctx = cairo.Context(surface)
    ctx.rotate(math.pi / 2)

    def measure(px):
        layout = _layout(ctx, text, family, px, column)
        _, logical = layout.get_extents()
        width = pango.units_to_double(logical.width)
        height = pango.units_to_double(logical.height)
        return layout, width, height

    low, high = 1.0, max(2.0, max_px)
    best = None
    layout, width, height = measure(high)
    if width <= column + 0.5 and height <= span:
        best = (high, layout, width, height)
    else:
        for _ in range(14):
            mid = (low + high) / 2
            layout, width, height = measure(mid)
            if width <= column + 0.5 and height <= span:
                low, best = mid, (mid, layout, width, height)
            else:
                high = mid
        if best is None:
            best = (low,) + measure(low)
    px, layout, width, height = best
    with BitmapTiles(canvas) as tiles:
        c = tiles.ctx
        c.save()
        # Columns start at the top; the block is centred across the region.
        c.translate(right - (span - height) / 2, top)
        c.rotate(math.pi / 2)
        pangocairo.update_layout(c, layout)
        color = ink
        c.set_source_rgba(*(v / 255 for v in color[:3]), 1)
        pangocairo.show_layout(c, layout)
        c.restore()
    return px
