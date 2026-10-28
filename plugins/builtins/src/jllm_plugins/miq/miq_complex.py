# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Pango shaping/bidi and Cairo's bounded, fractional-pixel compositor."""

import ctypes
import ctypes.util
import threading
from collections import OrderedDict
from functools import lru_cache

import cairocffi as cairo
import pangocairocffi as pangocairo
import pangocffi as pango
from PIL import Image, ImageColor, ImageDraw

from .miq_fonts import font_at
from .miq_words import CUSTOM, GRAPHEME, JAPANESE, runs, units

_FONTCONFIG_LOCK = threading.Lock()


@lru_cache(maxsize=32)
def register_font(path):
    fc = ctypes.CDLL(ctypes.util.find_library("fontconfig"))
    fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fc.FcConfigAppFontAddFile.restype = ctypes.c_int
    with _FONTCONFIG_LOCK:
        if not fc.FcConfigAppFontAddFile(None, str(path).encode()):
            raise ValueError(f"Fontconfigにフォントを登録できません: {path}")


def description(face, weight=None):
    family, style = face.getname()
    desc = pango.FontDescription()
    desc.family = family + ", Noto Sans"
    desc.set_absolute_size(pango.units_from_double(face.size))
    desc.weight = weight or (700 if "Bold" in style else 400)
    if "Italic" in style or "Oblique" in style:
        desc.style = pango.Style.ITALIC
    return desc


def to_surface(image):
    # Cairo uses premultiplied BGRA on little-endian Linux.
    data = bytearray(image.convert("RGBa").tobytes("raw", "BGRa"))
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, image.width, image.height
    ), data


def protect_words(attrs, text):
    """Forbid breaks inside each Japanese phrase from ``units``.

    Pango drops a break opportunity only when the characters on both sides lie
    in a no-break range, so each range must start at the phrase's first byte.
    """

    offset = 0
    for paragraph in text.split("\n"):
        for word in units(paragraph):
            end = offset + len(word.encode())
            if len(GRAPHEME.findall(word)) > 1:
                attr = pango.Attribute.from_pointer(pango.pango.pango_attr_allow_breaks_new(False))
                attr.start_index = offset
                attr.end_index = end
                attrs.insert(attr)
            offset = end
        offset += 1


class BitmapTiles:
    def __init__(self, canvas):
        self.canvas = canvas
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *canvas.size)
        self.ctx = cairo.Context(self.surface)
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def remember(self, key, value, cost):
        self.cache[key] = (value, cost)
        self.cache_bytes += cost
        while self.cache_bytes > 8 * 1024 * 1024:
            _, (_, removed) = self.cache.popitem(last=False)
            self.cache_bytes -= removed

    def text(self, face, text, fill, x, y, scale):
        key = ("text", id(face), text, str(fill))
        cached = self.cache.get(key)
        if cached is None:
            l, t, r, b = face.getbbox(text, anchor="ls")
            image = Image.new("RGBA", (max(1, r - l + 4), max(1, b - t + 4)))
            ImageDraw.Draw(image).text((2 - l, 2 - t), text, font=face, fill=fill, anchor="ls")
            surface, data = to_surface(image)
            value = (surface, data, l - 2, t - 2)
            self.remember(key, value, len(data))
        else:
            value = cached[0]
            self.cache.move_to_end(key)
        surface, data, left, top = value
        self.paint_surface(surface, x + left * scale, y + top * scale, scale)

    def __enter__(self):
        return self

    def paint(self, image, x, y, scale):
        key = ("image", id(image))
        cached = self.cache.get(key)
        if cached is None:
            surface, data = to_surface(image)
            self.remember(key, (surface, data, image), len(data) * 2)
        else:
            surface, data, _ = cached[0]
            self.cache.move_to_end(key)
        self.paint_surface(surface, x, y, scale)

    def paint_surface(self, surface, x, y, scale):
        self.ctx.save()
        self.ctx.translate(x, y)
        self.ctx.scale(scale, scale)
        self.ctx.set_source_surface(surface)
        self.ctx.get_source().set_filter(cairo.FILTER_BEST)
        self.ctx.paint()
        self.ctx.restore()

    def compose(self):
        """Composite the surface onto the canvas without a PNG round-trip."""

        self.surface.flush()
        premultiplied = Image.frombuffer(
            "RGBa",
            (self.surface.get_width(), self.surface.get_height()),
            self.surface.get_data(),
            "raw",
            "BGRa",
            self.surface.get_stride(),
            1,
        )
        self.canvas.alpha_composite(premultiplied.convert("RGBA"))

    def __exit__(self, *exc):
        if exc[0] is None:
            self.compose()
        self.surface.finish()
        self.cache.clear()


class ComplexText:
    def __init__(self, font):
        self.font = font
        for face in (font.font, font.italic):
            if face:
                register_font(str(face.path))
        if font.emoji_font:
            register_font(font.emoji_font)
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        self.ctx = cairo.Context(self.surface)
        context = pangocairo.create_context(self.ctx)
        fontmap = pango.pango.pango_context_get_font_map(context.pointer)
        pango.pango.pango_font_map_changed(fontmap)

    def layout(self, text, width=None):
        font = self.font
        layout = pangocairo.create_layout(self.ctx)
        layout.font_description = description(font.font, 800 if font.editorial else None)
        layout.wrap = pango.WrapMode.WORD
        attrs = pango.AttrList()
        result = []
        images = []
        offset = 0
        for token, kind in runs(text, font.editorial):
            custom = CUSTOM.fullmatch(token)
            image = font.images.get(token) if custom else None
            original = token
            if custom:
                token = "\ufffc" if image is not None else font.images.label(token)
            end = offset + len(token.encode())
            if image is not None:
                rect = pango.Rectangle()
                rect.x = 0
                rect.y = pango.units_from_double(-font.emoji_size() * 0.88)
                rect.width = pango.units_from_double(font._emoji_advance)
                rect.height = pango.units_from_double(font.emoji_size())
                attrs.insert(pango.Attribute.from_shape(rect, rect, offset, end))
                images.append((offset, image, original, end - offset))
            elif custom:
                images.append((offset, None, original, end - offset))
            elif kind == "latin" and font.italic:
                attrs.insert(
                    pango.Attribute.from_font_desc(
                        description(font.italic, 700 if font.editorial else None),
                        offset,
                        end,
                    )
                )
            elif kind == "emoji" and not custom and font.emoji_font:
                family = font_at(font.emoji_font, 109).getname()[0]
                attrs.insert(pango.Attribute.from_family(family, offset, end))
            result.append(token)
            offset = end
        processed = "".join(result)
        # Pango supplies script-specific breaks; protect Japanese phrases as well.
        if JAPANESE.search(processed):
            protect_words(attrs, processed)
        layout.text = processed
        layout.attributes = attrs
        if width is not None:
            layout.width = min(2_000_000_000, pango.units_from_double(width))
        missing = pango.pango.pango_layout_get_unknown_glyphs_count(layout.pointer)
        if missing:
            raise ValueError(
                f"{missing}文字のフォントが不足しています。fonts-noto-core / fonts-noto-extra / fonts-noto-cjk を導入してください。"
            )
        return layout, images

    def measure(self, text):
        layout, _ = self.layout(text)
        ink, logical = layout.get_extents()
        u = pango.units_to_double
        baseline = u(layout.get_baseline())
        return u(logical.width), (
            u(ink.x),
            u(ink.y) - baseline,
            u(ink.x + ink.width),
            u(ink.y + ink.height) - baseline,
        )

    def wrap(self, text, width):
        layout, images = self.layout(text, width)
        data = layout.text.encode()
        originals = {offset: (original, length) for offset, _, original, length in images}
        rows = []
        for i in range(layout.get_line_count()):
            line = pango.pango.pango_layout_get_line_readonly(layout.pointer, i)
            start = pango.pango.pango_layout_line_get_start_index(line)
            end = start + pango.pango.pango_layout_line_get_length(line)
            row = []
            offset = start
            skip_until = start
            for char in data[start:end].decode():
                if offset in originals:
                    original, length = originals[offset]
                    row.append(original)
                    skip_until = offset + length
                elif offset >= skip_until:
                    row.append(char)
                offset += len(char.encode())
            rows.append("".join(row).rstrip("\n"))
        return rows

    def draw(self, canvas, text, xy, fill, scale=1, *, emoji=True):
        layout, images = self.layout(text)
        baseline = pango.units_to_double(layout.get_baseline())
        with BitmapTiles(canvas) as tiles:
            ctx = tiles.ctx
            ctx.save()
            ctx.translate(xy[0], xy[1] - baseline * scale)
            ctx.scale(scale, scale)
            color = ImageColor.getrgb(fill) if isinstance(fill, str) else fill
            ctx.set_source_rgba(
                *(c / 255 for c in color[:3]), color[3] / 255 if len(color) > 3 else 1
            )
            pangocairo.show_layout(ctx, layout)
            ctx.restore()
            for offset, image, _, _ in images:
                if image is None or not emoji:
                    continue
                rect = pango.Rectangle()
                pango.pango.pango_layout_index_to_pos(layout.pointer, offset, rect.pointer)
                x = pango.units_to_double(min(rect.x, rect.x + rect.width))
                size = self.font.emoji_size()
                ratio = size / max(image.size)
                y = xy[1] - size * 0.88 * scale
                tiles.paint(image, xy[0] + x * scale, y, scale * ratio)
