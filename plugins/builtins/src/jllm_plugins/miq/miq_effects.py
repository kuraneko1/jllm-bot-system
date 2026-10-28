# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Image treatments shared by the layouts.

Everything here is built from Pillow's C operations; expensive full-canvas work
(blur, noise, gradients) runs on a reduced raster and is memoised per size.
"""

from __future__ import annotations

import colorsys
import math
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

SUPERSAMPLE = 4


def rgb(value):
    """``#rrggbb`` or a tuple to an ``(r, g, b)`` tuple."""

    if isinstance(value, str):
        value = value.lstrip("#")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    return tuple(value[:3])


def mix(a, b, t):
    a, b = rgb(a), rgb(b)
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def luminance(color):
    r, g, b = (c / 255 for c in rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@lru_cache(maxsize=64)
def _shape_mask(size, radius, kind):
    w, h = size
    if kind != "ellipse":
        return _rounded_mask(w, h, radius)
    big = Image.new("L", (w * SUPERSAMPLE, h * SUPERSAMPLE))
    draw = ImageDraw.Draw(big)
    box = (0, 0, big.width - 1, big.height - 1)
    draw.ellipse(box, fill=255)
    return big.resize(size, Image.Resampling.BOX)


def _rounded_mask(w, h, radius):
    """Only the four corners need supersampling; the rest is solid.

    Each corner tile is drawn from the same full-size shape (translated), so
    the result is identical to supersampling the whole rectangle.
    """

    n = SUPERSAMPLE
    cw, ch = min(radius + 2, -(-w // 2)), min(radius + 2, -(-h // 2))
    mask = Image.new("L", (w, h), 255)
    if radius <= 0:
        return mask
    box = (0, 0, w * n - 1, h * n - 1)
    for x in {0, w - cw}:
        for y in {0, h - ch}:
            tile = Image.new("L", (cw * n, ch * n))
            ImageDraw.Draw(tile).rounded_rectangle(
                (box[0] - x * n, box[1] - y * n, box[2] - x * n, box[3] - y * n),
                radius * n,
                fill=255,
            )
            mask.paste(tile.resize((cw, ch), Image.Resampling.BOX), (x, y))
    return mask


def round_mask(size, radius):
    """Antialiased rounded rectangle (copied: callers may draw on it)."""

    size = (max(1, round(size[0])), max(1, round(size[1])))
    return _shape_mask(size, max(0, round(radius)), "rounded").copy()


def ellipse_mask(size):
    size = (max(1, round(size[0])), max(1, round(size[1])))
    return _shape_mask(size, 0, "ellipse").copy()


def masked(image, mask):
    """Apply ``mask`` on top of the image's own alpha."""

    image = image.copy()
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


def _shrink(image, size):
    """Cheap integer pre-reduction of big photos before a LANCZOS resize."""

    factor = int(min(image.width / max(1, size[0]), image.height / max(1, size[1])) // 2)
    return image.reduce(factor) if factor >= 2 else image


def _method(image, size):
    """LANCZOS when shrinking; enlarging small avatars looks the same with the
    much cheaper bicubic filter."""

    if image.width < size[0] and image.height < size[1]:
        return Image.Resampling.BICUBIC
    return Image.Resampling.LANCZOS


def cover(image, size, centering=(0.5, 0.5)):
    size = (max(1, round(size[0])), max(1, round(size[1])))
    image = _shrink(image, size)
    return ImageOps.fit(image, size, _method(image, size), centering=centering)


def contain(image, size):
    size = (max(1, round(size[0])), max(1, round(size[1])))
    scale = min(size[0] / image.width, size[1] / image.height)
    image = _shrink(image, (image.width * scale, image.height * scale))
    return ImageOps.contain(
        image, size, _method(image, (image.width * scale, image.height * scale))
    )


def fitted(image, size, attachment):
    """Square-ish slots crop avatars; attachments keep their full frame."""

    return contain(image, size) if attachment else cover(image, size)


def soft_blur(image, radius):
    """A large Gaussian blur computed on a reduced copy (visually identical)."""

    if radius <= 0:
        return image
    factor = max(1, int(radius // 6))
    if factor == 1:
        return image.filter(ImageFilter.GaussianBlur(radius))
    small = image.resize(
        (max(1, image.width // factor), max(1, image.height // factor)),
        Image.Resampling.BOX,
    )
    small = small.filter(ImageFilter.GaussianBlur(radius / factor))
    return small.resize(image.size, Image.Resampling.BICUBIC)


def shadow(canvas, mask, xy, *, blur, color=(0, 0, 0), opacity=0.45, offset=(0, 0)):
    """Drop a blurred shadow of ``mask`` placed at ``xy``."""

    pad = round(blur * 3) + 2
    alpha = Image.new("L", (mask.width + pad * 2, mask.height + pad * 2))
    alpha.paste(mask, (pad, pad))
    alpha = soft_blur(alpha, blur).point(lambda v: round(v * opacity))
    layer = Image.new("RGBA", alpha.size, rgb(color) + (0,))
    layer.putalpha(alpha)
    canvas.alpha_composite(
        layer,
        (round(xy[0] + offset[0] - pad), round(xy[1] + offset[1] - pad)),
    )


@lru_cache(maxsize=4)
def _noise_tile(sigma):
    """A fixed (seeded) noise tile, so renders repeat exactly across processes."""

    import random

    side = 512
    tile = Image.frombytes("L", (side, side), random.Random(20240601).randbytes(side * side))
    spread = min(1.0, sigma / 128)
    return tile.point(lambda v: round(128 + (v - 128) * spread))


@lru_cache(maxsize=8)
def _noise(size, sigma):
    tile = _noise_tile(sigma)
    field = Image.new("L", size)
    for y in range(0, size[1], tile.height):
        for x in range(0, size[0], tile.width):
            field.paste(tile, (x, y))
    return field


def noise(size, sigma=64):
    return _noise(tuple(size), sigma).copy()


def grain(canvas, amount=0.06, sigma=48):
    """Film grain: a cached noise field blended in softly."""

    noise = _noise(canvas.size, sigma).convert("RGBA")
    noise.putalpha(round(255 * amount))
    canvas.alpha_composite(noise)


def _ramp_lut(colors, stops):
    """256-entry colour ramp through ``colors`` at ``stops`` (0..1)."""

    lut = []
    for i in range(256):
        t = i / 255
        for j in range(len(stops) - 1):
            if t <= stops[j + 1] or j == len(stops) - 2:
                span = max(1e-6, stops[j + 1] - stops[j])
                lut.append(mix(colors[j], colors[j + 1], min(1, max(0, (t - stops[j]) / span))))
                break
    return lut


def _apply_ramp(field, lut):
    """Map an ``L`` image through a colour ramp (pure C per pixel)."""

    bands = [field.point([c[k] for c in lut]) for k in range(3)]
    return Image.merge("RGB", bands).convert("RGBA")


FIELD = 64


@lru_cache(maxsize=32)
def _gradient(size, colors, angle, steps):
    w, h = size
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    # Project the canvas corners so the ramp spans the full rectangle.
    corners = [x * dx * w + y * dy * h for x in (0, 1) for y in (0, 1)]
    low, high = min(corners), max(corners)
    # Only the 0..1 position field is computed in Python, on a tiny raster.
    sw, sh = min(w, FIELD), min(h, FIELD)
    field = Image.new("L", (sw, sh))
    field.putdata(
        [
            round(
                255
                * min(
                    1,
                    max(
                        0,
                        (((x + 0.5) / sw) * dx * w + ((y + 0.5) / sh) * dy * h - low)
                        / max(1e-6, high - low),
                    ),
                )
            )
            for y in range(sh)
            for x in range(sw)
        ]
    )
    field = field.resize(size, Image.Resampling.BILINEAR)
    stops = steps or tuple(i / (len(colors) - 1) for i in range(len(colors)))
    return _apply_ramp(field, _ramp_lut(colors, stops))


def gradient(size, colors, angle=0.0, *, steps=None):
    """Smooth multi-stop linear gradient (``angle`` in degrees, 0 = left→right)."""

    size = (max(1, round(size[0])), max(1, round(size[1])))
    colors = tuple(rgb(c) for c in colors)
    return _gradient(size, colors, float(angle), tuple(steps) if steps else None).copy()


@lru_cache(maxsize=16)
def _radial(size, inner, outer, center, radius):
    w, h = size
    sw, sh = min(w, FIELD), min(h, FIELD)
    aspect = w / h
    data = []
    for y in range(sh):
        for x in range(sw):
            dx = ((x + 0.5) / sw - center[0]) * aspect
            dy = (y + 0.5) / sh - center[1]
            t = min(1, math.hypot(dx, dy) / (radius * max(1, aspect)))
            data.append(round(255 * t * t * (3 - 2 * t)))
    field = Image.new("L", (sw, sh))
    field.putdata(data)
    field = field.resize(size, Image.Resampling.BILINEAR)
    return _apply_ramp(field, _ramp_lut((inner, outer), (0, 1)))


def radial(size, inner, outer, center=(0.5, 0.5), radius=0.75):
    """Radial gradient from ``inner`` at ``center`` to ``outer``."""

    size = (max(1, round(size[0])), max(1, round(size[1])))
    return _radial(size, rgb(inner), rgb(outer), tuple(center), radius).copy()


def blobs(size, specs, blur):
    """Soft colour fields (aurora/mesh gradients). ``specs``: (x, y, rx, ry, rgba)."""

    return _blobs(tuple(size), tuple(map(tuple, specs)), blur).copy()


@lru_cache(maxsize=16)
def _blobs(size, specs, blur):
    w, h = size
    factor = 8
    small = Image.new("RGBA", (max(1, w // factor), max(1, h // factor)), (0, 0, 0, 0))
    for x, y, rx, ry, color in specs:
        layer = Image.new("RGBA", small.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse(
            (
                (x - rx) / factor,
                (y - ry) / factor,
                (x + rx) / factor,
                (y + ry) / factor,
            ),
            fill=color,
        )
        small.alpha_composite(layer)
    small = small.filter(ImageFilter.GaussianBlur(max(1, blur / factor)))
    return small.resize(size, Image.Resampling.BICUBIC)


def backdrop(image, size, blur, *, darken=0.0, saturation=1.0):
    """Full-bleed blurred photo, the way music players dress their screens."""

    w, h = size
    small = cover(image, (max(1, w // 10), max(1, h // 10))).convert("RGB")
    small = small.filter(ImageFilter.GaussianBlur(max(1, blur / 10)))
    if saturation != 1.0:
        from PIL import ImageEnhance

        small = ImageEnhance.Color(small).enhance(saturation)
    if darken:
        small = Image.blend(small, Image.new("RGB", small.size), darken)
    return small.resize(size, Image.Resampling.BICUBIC).convert("RGBA")


def grayscale(image, contrast=1.0):
    alpha = image.getchannel("A")
    gray = ImageOps.grayscale(image)
    if contrast != 1.0:
        gray = ImageOps.autocontrast(gray, cutoff=1)
        from PIL import ImageEnhance

        gray = ImageEnhance.Contrast(gray).enhance(contrast)
    out = gray.convert("RGBA")
    out.putalpha(alpha)
    return out


def duotone(image, dark, light, mid=None):
    alpha = image.getchannel("A")
    gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
    if mid is None:
        out = ImageOps.colorize(gray, rgb(dark), rgb(light))
    else:
        out = ImageOps.colorize(gray, rgb(dark), rgb(light), rgb(mid))
    out = out.convert("RGBA")
    out.putalpha(alpha)
    return out


@lru_cache(maxsize=8)
def _screen(cell, angle):
    """One period of an AM halftone screen: dot threshold rising from the centre."""

    n = max(2, cell)
    tile = Image.new("L", (n, n))
    data = []
    for y in range(n):
        for x in range(n):
            dx, dy = (x + 0.5) / n - 0.5, (y + 0.5) / n - 0.5
            d = math.hypot(dx, dy) / math.hypot(0.5, 0.5)
            data.append(round(255 * min(1, d)))
    tile.putdata(data)
    return tile


@lru_cache(maxsize=8)
def _screen_field(size, cell, angle):
    tile = _screen(cell, angle)
    diag = math.ceil(math.hypot(*size)) + cell * 2
    field = Image.new("L", (diag, diag))
    for y in range(0, diag, tile.height):
        for x in range(0, diag, tile.width):
            field.paste(tile, (x, y))
    field = field.rotate(angle, resample=Image.Resampling.BILINEAR)
    left = (diag - size[0]) // 2
    top = (diag - size[1]) // 2
    return field.crop((left, top, left + size[0], top + size[1]))


def halftone(image, cell, *, angle=45, ink=(16, 16, 16), paper=None, contrast=1.15):
    """Newspaper/manga dots: a threshold screen, so it is pure C per pixel."""

    alpha = image.getchannel("A")
    gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=2)
    if contrast != 1:
        from PIL import ImageEnhance

        gray = ImageEnhance.Contrast(gray).enhance(contrast)
    darkness = ImageOps.invert(gray)
    screen = _screen_field(image.size, max(2, round(cell)), angle)
    # Ink where the darkness exceeds the screen threshold; a small blur
    # before thresholding keeps the dot edges antialiased.
    dots = ImageChops.subtract(darkness, screen, scale=1 / 6)
    dots = dots.point(lambda v: min(255, v * 2))
    layer = Image.new("RGBA", image.size, rgb(ink) + (0,))
    layer.putalpha(ImageChops.multiply(dots, alpha))
    if paper is None:
        return layer
    base = Image.new("RGBA", image.size, rgb(paper) + (255,))
    base.putalpha(alpha)
    base.alpha_composite(layer)
    return base


def pixelate(image, cells, colors=24):
    """Nearest-neighbour pixel art with a reduced palette."""

    w, h = image.size
    cells = max(4, cells)
    small = image.resize((cells, max(1, round(cells * h / w))), Image.Resampling.BOX)
    alpha = small.getchannel("A").point(lambda v: 255 if v > 96 else 0)
    quant = small.convert("RGB").quantize(colors, dither=Image.Dither.NONE).convert("RGB")
    out = quant.convert("RGBA")
    out.putalpha(alpha)
    return out.resize((w, h), Image.Resampling.NEAREST)


def line_art(image, color=(255, 255, 255), strength=3.0):
    """Edge drawing for technical/blueprint looks."""

    gray = ImageOps.grayscale(image).filter(ImageFilter.GaussianBlur(1.2))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges, cutoff=2).point(lambda v: min(255, round(v * strength)))
    edges = ImageChops.multiply(edges, image.getchannel("A"))
    layer = Image.new("RGBA", image.size, rgb(color) + (0,))
    layer.putalpha(edges)
    return layer


def rgb_split(image, shift):
    """Chromatic aberration: red and blue planes pulled apart horizontally."""

    r, g, b, a = image.split()
    shift = max(1, round(shift))
    r = ImageChops.offset(r, -shift, 0)
    b = ImageChops.offset(b, shift, 0)
    return Image.merge("RGBA", (r, g, b, a))


@lru_cache(maxsize=8)
def _scanlines(size, period, strength):
    w, h = size
    column = Image.new("L", (1, h))
    column.putdata([round(255 * strength) if (y % period) < period / 2 else 0 for y in range(h)])
    return column.resize(size, Image.Resampling.NEAREST)


def scanlines(canvas, period, strength=0.25, color=(0, 0, 0)):
    layer = Image.new("RGBA", canvas.size, rgb(color) + (0,))
    layer.putalpha(_scanlines(canvas.size, max(2, round(period)), strength))
    canvas.alpha_composite(layer)


def palette(image, count=5):
    """Dominant colours, most frequent first (tiny copy, so it is cheap)."""

    small = image.convert("RGBA").resize((48, 48), Image.Resampling.BOX)
    background = Image.new("RGBA", small.size, (128, 128, 128, 255))
    background.alpha_composite(small)
    quant = background.convert("RGB").quantize(count, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()[: count * 3]
    counts = sorted(quant.getcolors(), reverse=True)
    return [tuple(pal[i * 3 : i * 3 + 3]) for _, i in counts]


def accent(image, *, minimum_saturation=0.28, value=(0.35, 0.9)):
    """The most characteristic colour, normalised so it works as a flat fill."""

    colors = palette(image, 6)
    best, score = colors[0], -1
    for i, color in enumerate(colors):
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in color))
        candidate = s * (1 - i * 0.08) + (0.2 if 0.25 < v < 0.95 else 0)
        if candidate > score:
            best, score = color, candidate
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in best))
    s = max(s, minimum_saturation) if s > 0.08 else s
    v = min(max(v, value[0]), value[1])
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def shade(color, hue_shift=0.0, saturation=1.0, value=1.0):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb(color)))
    h = (h + hue_shift) % 1
    s = min(1, max(0, s * saturation))
    v = min(1, max(0, v * value))
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def ink_for(background, light=(250, 250, 248), dark=(18, 18, 20)):
    return dark if luminance(background) > 0.58 else light


def colored(mask, color):
    """A solid colour layer shaped by an ``L`` mask."""

    layer = Image.new("RGBA", mask.size, rgb(color) + (0,))
    alpha = mask
    if len(color) == 4:
        alpha = mask.point(lambda v: round(v * color[3] / 255))
    layer.putalpha(alpha)
    return layer


def glow(mask, color, radius, strength=1.0):
    """Bloom around text or strokes: blurred copies of the mask, boosted."""

    halo = soft_blur(mask, radius)
    if strength != 1.0:
        halo = halo.point(lambda v: min(255, round(v * strength)))
    return colored(halo, color)


def outline(mask, width):
    """Dilate a mask (for stickers and outlined lettering)."""

    width = max(1, round(width))
    if width <= 3:
        return mask.filter(ImageFilter.MaxFilter(width * 2 + 1))
    # Wide outlines: a blur pushed back to full strength grows the shape with
    # soft (antialiased) edges, far cheaper than a large rank filter.
    grown = soft_blur(mask, width * 0.55)
    return grown.point(lambda v: min(255, v * 6))


def star(draw, cx, cy, r, fill, points=4, inner=0.42, rotation=0.0):
    """A sparkle (4 points) or star (5 points)."""

    coords = []
    for i in range(points * 2):
        angle = rotation + math.pi * i / points - math.pi / 2
        radius = r if i % 2 == 0 else r * inner
        coords.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    draw.polygon(coords, fill=fill)


def heart(draw, cx, cy, size, fill):
    """A heart from two circles and a triangle."""

    r = size / 4
    draw.ellipse((cx - 2 * r, cy - r * 1.6, cx, cy + r * 0.4), fill=fill)
    draw.ellipse((cx, cy - r * 1.6, cx + 2 * r, cy + r * 0.4), fill=fill)
    draw.polygon(
        [
            (cx - 2 * r + r * 0.06, cy - r * 0.3),
            (cx + 2 * r - r * 0.06, cy - r * 0.3),
            (cx, cy + r * 2.1),
        ],
        fill=fill,
    )


def supersampled(size, painter, factor=SUPERSAMPLE):
    """Draw vector shapes at ``factor``× and downsample for clean edges."""

    big = Image.new("RGBA", (size[0] * factor, size[1] * factor), (0, 0, 0, 0))
    painter(ImageDraw.Draw(big), factor)
    return big.resize(size, Image.Resampling.LANCZOS)
