# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Aspect-preserving media placement, cached masks and C-level image effects."""

import colorsys
import os
import random
import struct
import zlib
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

LANCZOS = Image.Resampling.LANCZOS


def _ramp(values, size) -> Image.Image:
    return Image.frombytes("L", size, bytes(values))


def _smooth(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


@lru_cache(maxsize=8)
def _edge_mask(size: tuple[int, int], fade) -> Image.Image:
    def axis(n):
        values = []
        for pos in range(n):
            t = min(1, max(0, min(pos, n - 1 - pos) / fade))
            values.append(round(255 * t * t * (3 - 2 * t)))
        return _ramp(values, (n, 1))

    w, h = size
    return ImageChops.multiply(
        axis(w).resize(size), axis(h).transpose(Image.Transpose.ROTATE_90).resize(size)
    )


def edge_mask(size, fade):
    """The fade only depends on the rectangle, so the raster is memoised."""

    return _edge_mask(tuple(size), fade).copy()


def read_image(path, hint=None):
    """Decode once; JPEG decodes directly at a reduced scale when ``hint`` allows.

    Opaque images stay RGB: every later resize and composite is ~2-3x cheaper
    than in RGBA, where Pillow premultiplies around each resample.
    """

    with Image.open(path) as source:
        if hint and source.format == "JPEG":
            source.draft("RGB", (hint[0] * 2, hint[1] * 2))
        image = ImageOps.exif_transpose(source)
        if image.mode == "RGB":
            return image.copy() if image is source else image
        image = image.convert("RGBA")
    low, _ = image.getchannel("A").getextrema()
    return image.convert("RGB") if low == 255 else image


def put(canvas, image, xy=(0, 0)):
    """Composite onto an RGB canvas (negative offsets allowed)."""

    xy = (round(xy[0]), round(xy[1]))
    if image.mode == "RGB":
        canvas.paste(image, xy)
    else:
        canvas.paste(image, xy, image)


def _resize(image, size, box=None, fast=False):
    size = (max(1, round(size[0])), max(1, round(size[1])))
    if image.size == size and box is None:
        return image.copy()
    source = (box[2] - box[0], box[3] - box[1]) if box else image.size
    shrink = min(source[0] / size[0], source[1] / size[1])
    if fast:
        resample = Image.Resampling.BILINEAR
    elif shrink > 1.25:
        resample = LANCZOS
    else:
        # Enlarging or near 1:1: bicubic is indistinguishable and ~3x faster.
        resample = Image.Resampling.BICUBIC
    return image.resize(size, resample, box=box, reducing_gap=2.0 if shrink > 2 else None)


def cover(image, size, centering=(0.5, 0.5), fast=False):
    """Fill ``size`` completely, cropping the overflow around ``centering``."""

    w, h = size
    iw, ih = image.size
    scale = max(w / iw, h / ih)
    cw, ch = w / scale, h / scale
    left = (iw - cw) * centering[0]
    top = (ih - ch) * centering[1]
    return _resize(image, size, (left, top, left + cw, top + ch), fast)


def contain(image, size):
    iw, ih = image.size
    scale = min(size[0] / iw, size[1] / ih)
    return _resize(image, (iw * scale, ih * scale))


def fit_media(image, size, attachment=False, centering=(0.5, 0.5)):
    """Icons fill their slot; attachments keep their whole frame."""

    return contain(image, size) if attachment else cover(image, size, centering)


def opaque(image, color=(0, 0, 0)):
    if image.mode == "RGB":
        return image
    base = Image.new("RGB", image.size, color)
    base.paste(image, (0, 0), image)
    return base


def backdrop(image, size, attachment=False, centering=(0.5, 0.4), fast=False):
    """A full-bleed RGB picture; an attachment sits on a blurred copy of itself."""

    if not attachment:
        return opaque(cover(image, size, centering, fast))
    small = (max(1, size[0] // 4), max(1, size[1] // 4))
    canvas = tint(opaque(soft_blur(cover(image, small, fast=True), 6)), 0.6)
    canvas = canvas.resize(size, Image.Resampling.BILINEAR)
    front = contain(image, size)
    put(canvas, front, ((size[0] - front.width) // 2, (size[1] - front.height) // 2))
    return canvas


def soft_blur(image, radius):
    """Blur at a fraction of the size: the result is identical to the eye, far faster."""

    factor = max(1, int(radius // 3))
    small = image.reduce(factor) if factor > 1 else image
    small = small.filter(ImageFilter.GaussianBlur(radius / factor))
    return small.resize(image.size, Image.Resampling.BILINEAR)


def _keep_alpha(out, image):
    if image.mode == "RGBA":
        out.putalpha(image.getchannel("A"))
    return out


def grayscale(image, contrast=1.0):
    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    if contrast != 1.0:
        gray = gray.point(lambda v: max(0, min(255, round((v - 128) * contrast + 128))))
    return _keep_alpha(Image.merge("RGB", (gray, gray, gray)), image)


def duotone(image, dark, light):
    gray = ImageOps.autocontrast(image.convert("L"), cutoff=2)
    return _keep_alpha(ImageOps.colorize(gray, dark[:3], light[:3]), image)


def tint(image, factor):
    """Multiply the colour by ``factor`` (0..1) while keeping alpha."""

    table = [min(255, round(v * factor)) for v in range(256)]
    if image.mode == "RGB":
        return image.point(table * 3)
    return image.point(table * 3 + list(range(256)))


def pixelate(image, size, cells):
    small = cover(image, (cells, max(1, round(cells * size[1] / size[0]))))
    return small.resize(size, Image.Resampling.NEAREST)


@lru_cache(maxsize=8)
def _screen(size, cell):
    """A 45-degree dot screen: distance to the nearest dot centre, tiled in C.

    A square lattice rotated by 45 degrees is a square tile with dots at its
    corners and centre, so one small tile describes the whole screen.
    """

    tile = Image.new("L", (cell, cell))
    centres = [(0, 0), (cell, 0), (0, cell), (cell, cell), (cell / 2, cell / 2)]
    limit = cell / 2**1.5 * 1.42
    tile.putdata(
        [
            min(
                255,
                round(
                    255
                    * min(((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5 for cx, cy in centres)
                    / limit
                ),
            )
            for y in range(cell)
            for x in range(cell)
        ]
    )
    screen = Image.new("L", size)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            screen.paste(tile, (x, y))
    return screen


def halftone(image, cell, ink=(20, 20, 20), paper=(240, 236, 226)):
    """Amplitude-modulated dots, thresholded in C against a cached screen."""

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=2)
    gray = gray.resize((image.width * 2, image.height * 2), Image.Resampling.BILINEAR)
    dark = ImageOps.invert(gray)
    dots = ImageChops.subtract(dark, _screen(dark.size, cell * 2), scale=1.0 / 64).point(
        lambda v: 255 if v else 0
    )
    dots = dots.resize(image.size, Image.Resampling.BOX)
    out = Image.new("RGB", image.size, paper)
    out.paste(ink, (0, 0) + image.size, dots)
    return _keep_alpha(out, image)


@lru_cache(maxsize=16)
def fade_mask(size, start, end, slant=0.0, invert=False):
    """Opaque before ``start`` and clear after ``end`` (fractions of the width).

    ``slant`` moves the edge by that fraction of the width from top to bottom, so
    the boundary runs diagonally. One ramp row is sheared in C, not per pixel.
    """

    w, h = size
    extra = round(abs(slant) * w)
    span = w + 2 * extra
    width = max(1.0, (end - start) * w)
    ramp = [round(255 * (1 - _smooth((i - extra - start * w) / width))) for i in range(span)]
    if invert:
        ramp = [255 - v for v in ramp]
    row = _ramp(ramp, (span, 1)).resize((span, h), Image.Resampling.NEAREST)
    if not extra:
        return row
    # Source x = x + extra - slant * w * y / h, always inside the ramp.
    return row.transform(
        (w, h),
        Image.Transform.AFFINE,
        (1, -slant * w / h, extra, 0, 1, 0),
        Image.Resampling.BILINEAR,
    )


@lru_cache(maxsize=16)
def vertical_mask(size, start, end, invert=False):
    """Clear above ``start`` and opaque below ``end`` (fractions of the height)."""

    w, h = size
    column = [round(255 * _smooth((y - start * h) / max(1, (end - start) * h))) for y in range(h)]
    if invert:
        column = [255 - v for v in column]
    return _ramp(column, (1, h)).resize((w, h), Image.Resampling.NEAREST)


def light_map(*layers):
    """Combine "keep" masks (255 = untouched) into one RGB multiplier.

    Several darkening overlays collapse into a single ``ImageChops.multiply``.
    """

    keep = layers[0]
    for layer in layers[1:]:
        keep = ImageChops.multiply(keep, layer)
    return Image.merge("RGB", (keep, keep, keep))


def keep_level(mask, amount):
    """Turn a darkening mask into a keep mask: 255 - amount * mask."""

    return mask.point([round(255 - amount * v) for v in range(256)])


@lru_cache(maxsize=8)
def vignette(size, strength=0.6):
    w, h = size
    small = Image.new("L", (64, 36), 0)
    ImageDraw.Draw(small).ellipse((-14, -10, 78, 46), fill=255)
    small = small.filter(ImageFilter.GaussianBlur(9))
    level = round(255 * (1 - strength))
    small = small.point(lambda v: level + (255 - level) * v // 255)
    return small.resize(size, Image.Resampling.BILINEAR)


@lru_cache(maxsize=8)
def grain(size, amount=14, seed=7):
    """Deterministic film grain as a mid-grey overlay layer (alpha carries it)."""

    rng = random.Random(seed)
    w, h = size
    tw, th = min(w, 256), min(h, 256)
    # Two-pixel grains read the same on screen and let PNG's Up filter skip rows.
    noise = Image.frombytes(
        "L", (tw // 2, th // 2), bytes(rng.getrandbits(8) for _ in range(tw // 2 * th // 2))
    ).resize((tw // 2 * 2, th // 2 * 2), Image.Resampling.NEAREST)
    tw, th = noise.size
    tile = Image.new("L", size)
    for y in range(0, h, th):
        for x in range(0, w, tw):
            tile.paste(noise, (x, y))
    layer = Image.merge("RGBA", (tile, tile, tile, Image.new("L", size, amount)))
    return layer


@lru_cache(maxsize=8)
def paper(size, color, seed=3):
    """Paper with soft fibres; built once per size."""

    w, h = size
    rng = random.Random(seed)
    sw, sh = max(1, w // 16), max(1, h // 16)
    noise = Image.frombytes("L", (sw, sh), bytes(rng.randrange(114, 142) for _ in range(sw * sh)))
    noise = noise.resize(size, Image.Resampling.BICUBIC)
    base = Image.new("RGB", size, color)
    base.paste(Image.merge("RGB", (noise, noise, noise)), (0, 0), Image.new("L", size, 40))
    return base


def dominant_color(image):
    """The most characterful colour of the icon (vivid, popular, not near-grey)."""

    small = image.convert("RGB").resize((24, 24), Image.Resampling.BILINEAR)
    colors = small.quantize(6, method=Image.Quantize.MEDIANCUT).convert("RGB").getcolors(576) or []
    best, score = (90, 120, 160), -1.0
    for count, rgb in colors:
        h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        value = count * (0.15 + s) * (1 - abs(l - 0.5))
        if value > score:
            best, score = rgb, value
    return best


def shade(rgb, lightness=None, saturation=None):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb[:3]))
    l = l if lightness is None else lightness
    s = s if saturation is None else saturation
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s))


def luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def rounded_mask(size, radius, scale=3):
    """Anti-aliased rounded rectangle (or circle when radius >= half the side).

    Shared and cached: callers must not modify the returned mask.
    """

    return _rounded_mask(tuple(size), round(radius), scale)


@lru_cache(maxsize=16)
def _rounded_mask(size, radius, scale):
    w, h = size
    big = Image.new("L", (w * scale, h * scale))
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1), radius * scale, fill=255
    )
    return big.resize(size, Image.Resampling.BOX)


def with_mask(image, mask):
    image = image.copy()
    if image.mode == "RGBA":
        mask = ImageChops.multiply(image.getchannel("A"), mask)
    image.putalpha(mask)
    return image


def place_image(canvas, image, slot, attachment=False):
    sw, sh = slot[2] - slot[0], slot[3] - slot[1]
    if attachment:
        image = contain(image, (sw, sh))
    else:
        side = min(sw, sh)
        image = cover(image, (side, side))
    xy = (slot[0] + (sw - image.width) // 2, slot[1] + (sh - image.height) // 2)
    image = with_mask(image, edge_mask(image.size, max(3, round(min(image.size) * 0.06))))
    put(canvas, image, xy)


def _chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


_POOL = None
_BANDS = max(1, min(4, os.cpu_count() or 1))


def _deflate(data, level, last):
    stream = zlib.compressobj(level, zlib.DEFLATED, -15, 9)
    return stream.compress(data) + stream.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)


def encode_png(image, level=1):
    """PNG with the ``Up`` filter computed in C and deflate split across threads.

    Pillow's encoder tries every filter per row and deflates on one core; here the
    filter is one C call and bands are compressed in parallel (zlib releases the
    GIL). Sync-flushed raw deflate blocks concatenate into one valid stream, as
    pigz does. Output is a standard, lossless RGB PNG.
    """

    global _POOL
    if image.mode != "RGB":
        image = image.convert("RGB")
    w, h = image.size
    stride = w * 3
    above = Image.new("RGB", (w, h))
    above.paste(image.crop((0, 0, w, h - 1)), (0, 1))
    up = ImageChops.subtract_modulo(image, above).tobytes()
    first = image.crop((0, 0, w, 1)).tobytes()
    rows = [b"\x00" + first]
    rows.extend(b"\x02" + up[i : i + stride] for i in range(stride, len(up), stride))
    bands = _BANDS if h >= 64 * _BANDS else 1
    step = -(-h // bands)
    parts = [b"".join(rows[i : i + step]) for i in range(0, h, step)]
    if len(parts) > 1:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor

            _POOL = ThreadPoolExecutor(_BANDS, thread_name_prefix="miq-png")
        futures = [
            _POOL.submit(_deflate, part, level, i == len(parts) - 1) for i, part in enumerate(parts)
        ]
        body = b"".join(f.result() for f in futures)
    else:
        body = _deflate(parts[0], level, True)
    checksum = 1
    for part in parts:
        checksum = zlib.adler32(part, checksum)
    data = b"\x78\x01" + body + struct.pack(">I", checksum)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)),
            _chunk(b"IDAT", data),
            _chunk(b"IEND", b""),
        )
    )


@lru_cache(maxsize=8)
def _classic_mask(canvas_size, image_size, xy):
    w, h = canvas_size
    kx, ky = w / 1920, h / 1080
    k = min(kx, ky)
    horizontal = []
    for x in range(w):
        t = max(0, min(1, (x - 555 * kx) / (405 * kx)))
        horizontal.append(int(255 * (1 - t * t * (3 - 2 * t))))
    top, bottom = round(65 * k), round(1035 * k)
    vertical = [
        max(0, int(255 * min(1, (y - top) / (92 * k), (bottom - y) / (115 * k))))
        if top <= y < bottom
        else 0
        for y in range(h)
    ]
    mask = ImageChops.multiply(
        _ramp(horizontal, (w, 1)).resize(canvas_size),
        _ramp(vertical, (1, h)).resize(canvas_size),
    )
    edges = Image.new("L", canvas_size)
    edges.paste(edge_mask(image_size, max(1, round(min(image_size) * 0.045))), xy)
    return ImageChops.multiply(mask, edges)


def classic_image(canvas, image, attachment=False):
    w, h = canvas.size
    kx, ky = w / 1920, h / 1080
    k = min(kx, ky)
    side = max(1, round(970 * k))
    if attachment:
        image = ImageOps.contain(image, (side, side), Image.Resampling.LANCZOS)
    else:
        image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
    xy = (
        round(-12 * kx) + (side - image.width) // 2,
        round(65 * k) + (side - image.height) // 2,
    )
    layer = Image.new("RGBA", canvas.size)
    layer.alpha_composite(image, xy)
    layer.putalpha(
        ImageChops.multiply(layer.getchannel("A"), _classic_mask(canvas.size, image.size, xy))
    )
    canvas.alpha_composite(layer)


def collage(images, size=1024, gap=12, background=(16, 16, 18, 255)):
    """Up to four pictures in one frame (one message with several attachments).

    One picture is returned as is; two sit side by side (or stacked when they
    are wide), three use one large and two small cells, four a 2x2 grid.
    """

    images = [image.convert("RGBA") for image in images[:4]]
    if not images:
        raise ValueError("画像がありません。")
    if len(images) == 1:
        return images[0]
    wide = sum(i.width / i.height for i in images) / len(images) > 1.3
    if len(images) == 2:
        cells = (
            [(0, 0, size, size // 2), (0, size // 2, size, size)]
            if wide
            else [(0, 0, size // 2, size), (size // 2, 0, size, size)]
        )
    elif len(images) == 3:
        half = size // 2
        cells = [(0, 0, half, size), (half, 0, size, half), (half, half, size, size)]
    else:
        half = size // 2
        cells = [(x, y, x + half, y + half) for y in (0, half) for x in (0, half)]
    canvas = Image.new("RGBA", (size, size), background)
    for image, (l, t, r, b) in zip(images, cells):
        l, t = l + (gap // 2 if l else 0), t + (gap // 2 if t else 0)
        r, b = r - (gap // 2 if r < size else 0), b - (gap // 2 if b < size else 0)
        canvas.alpha_composite(cover(image, (r - l, b - t)), (l, t))
    return canvas


def spoiler(image):
    """Hide a spoiler picture: heavy blur and a dark veil, like Discord."""

    small = image.convert("RGBA").resize(
        (max(1, image.width // 16), max(1, image.height // 16)), Image.Resampling.BOX
    )
    blurred = small.resize(image.size, Image.Resampling.BICUBIC)
    veil = Image.new("RGBA", image.size, (0, 0, 0, 110))
    return Image.alpha_composite(blurred, veil)
