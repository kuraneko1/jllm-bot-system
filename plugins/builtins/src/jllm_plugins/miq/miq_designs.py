# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Every layout. Coordinates are in the 1920x1080 design space (see Scene)."""

from __future__ import annotations

import math
import zlib
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from . import miq_effects as fx
from .miq_fonts import bundled_fonts
from .miq_images import classic_image, place_image
from .miq_layouts import fill, layout
from .miq_text import fit, paint

BASE_SIZE = (1920, 1080)
TEXT_LEFT = {"right": 1010, "center": 845}
TEXT_RIGHT = 1780
CENTER_SHIFT = 0.05

TEXT_FACES = ("gothic", "mincho", "maru", "shippori", "dela")
CLASSIC_FACES = ("gothic", "mincho", "maru", "shippori", "dela")


# ---------------------------------------------------------------------------
# classic: the original composition, painted natively at the output size
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _background_layer(size: tuple[int, int]) -> Image.Image:
    w, h = size
    kx, ky = w / BASE_SIZE[0], h / BASE_SIZE[1]
    bg = Image.new("RGB", size, (0, 1, 2))
    # Subtle cyan glow; no numpy needed.
    layer = Image.new("RGBA", size)
    gd = ImageDraw.Draw(layer)
    gd.ellipse((-320 * kx, -125 * ky, 1160 * kx, 1220 * ky), fill=(0, 70, 130, 26))
    layer = layer.filter(ImageFilter.GaussianBlur(max(1, int(255 * min(kx, ky)))))
    return Image.alpha_composite(bg.convert("RGBA"), layer)


def dark_background(size: tuple[int, int]) -> Image.Image:
    # The glow only depends on the canvas size; callers paint over their own copy.
    return _background_layer(tuple(size)).copy()


def text_region(width: int, height: int, has_credit: bool, align: str = "right"):
    """本文を置ける領域 ``(left, top, right, bottom)`` を返す。

    ``left`` は折り返しの左端。``center`` は上流と同じ広さを取り、``right`` は
    アイコンの右側だけに取る（長い行でもアイコンに掛からない）。
    """

    kx, ky = width / BASE_SIZE[0], height / BASE_SIZE[1]
    k = min(kx, ky)
    left = round(TEXT_LEFT.get(align, TEXT_LEFT["right"]) * kx)
    right = round(TEXT_RIGHT * kx)
    top = round(150 * k)
    bottom = (round(846 * ky) - round(80 * k)) if has_credit else (height - round(80 * k))

    return left, top, right, bottom


def line_anchor(width: int, left: int, right: int, widest: float, align: str = "right") -> float:
    """行の中心の x を決める。

    ``center`` は画面中央から少し右（``CENTER_SHIFT``）。ただしアイコン側（``left``）と
    右端（``right``）を越えない範囲で寄せるので、長い行でもアイコンに重ならない。
    """

    if align != "center":
        return (left + right) / 2

    # 画面中央から少しだけ右へ寄せた位置を狙う
    target = width * (0.5 + CENTER_SHIFT)
    low, high = left + widest / 2, right - widest / 2

    if low > high:
        return (left + right) / 2

    return min(max(target, low), high)


@layout("classic", "クラシック", "classic", CLASSIC_FACES, number=1, native=True)
def classic(s):
    from .miq_text import credit

    w, h = s.size
    kx, ky, k = s.kx, s.ky, s.k
    align = getattr(s, "align", "right")
    box = text_region(w, h, s.has_credit, align)
    canvas = dark_background(s.size)
    classic_image(canvas, s.image, s.attachment)
    if s.aa:
        from .miq_aa import draw

        draw(
            canvas,
            s.aa[1],
            s.aa[0],
            bundled_fonts(),
            box,
            (246, 248, 250),
            max(9, round(76 * k * s.family.scale)),
            align="center",
            valign="middle",
        )
        cx = line_anchor(w, box[0], box[2], 0, align)
    else:
        result = fit(
            s.text,
            s.quote_font,
            box[2] - box[0],
            box[3] - box[1],
            max(9, round(76 * k * s.family.scale)),
            classic=True,
        )
        widest = max(
            (result.font.getlength(row) * result.scale for row in result.lines),
            default=0,
        )
        cx = line_anchor(w, box[0], box[2], widest, align)
        paint(canvas, result, box, (246, 248, 250), center=True, classic=True, cx=cx)
    if s.has_credit:
        y = round(846 * ky)
        ImageDraw.Draw(canvas).line(
            (cx - round(142 * kx), y, cx + round(142 * kx), y),
            fill=(63, 125, 149),
            width=max(1, round(2 * k)),
        )
        name_size = max(9, round(43 * k))
        while name_size > 9 and s.name_font(name_size).width("— " + s.name) > box[2] - box[0]:
            name_size -= 1
        if s.name:
            credit(
                canvas,
                "— " + s.name,
                s.name_font,
                name_size,
                (cx, y + round(24 * ky)),
                box[2] - box[0],
                (248, 250, 252),
                center=True,
            )
        if s.handle:
            credit(
                canvas,
                s.handle_text,
                s.handle_font,
                max(8, round(30 * k)),
                (cx, y + round((88 if s.name else 45) * ky)),
                box[2] - box[0],
                (132, 160, 173),
                center=True,
            )
    return canvas


# ---------------------------------------------------------------------------
# The original editorial trio
# ---------------------------------------------------------------------------


def _editorial(s, background, ink, secondary, slot, text_box, maximum, author, center):
    canvas = fill(s.size, background)
    place_image(canvas, s.image, s.box(slot), s.attachment)
    s.write(canvas, text_box, ink, maximum, align="center" if center else "left")
    available = 1920 - 104 - author[0]
    s.credits(
        canvas,
        author,
        ink,
        secondary,
        name_px=48,
        handle_px=32,
        gap=62,
        width=available,
    )
    return canvas


@layout("mirrored", "ミラー", "classic", CLASSIC_FACES, number=2)
def mirrored(s):
    return _editorial(
        s,
        "#101010",
        "#f4f3ef",
        "#aaa9a7",
        (946, 65, 1896, 1015),
        (112, 150, 886, 766),
        82,
        (112, 926),
        False,
    )


@layout("magazine", "誌面", "classic", ("gothic", "mincho", "shippori", "dela"), number=3)
def magazine(s):
    return _editorial(
        s,
        "#f1f1ed",
        "#101010",
        "#626260",
        (1306, 184, 1816, 694),
        (112, 143, 1202, 970),
        88,
        (1306, 822),
        False,
    )


@layout("literary", "文芸", "classic", ("mincho", "shippori", "gothic", "klee"), number=4)
def literary(s):
    slot = (545, 804, 865, 984) if s.attachment else (710, 833, 858, 981)
    author = (930, 876) if s.attachment else (900, 889)
    return _editorial(
        s,
        "#f1f1ed",
        "#101010",
        "#626260",
        slot,
        (150, 154, 1770, 730),
        98,
        author,
        True,
    )


# ---------------------------------------------------------------------------
# Cool
# ---------------------------------------------------------------------------


@layout("noir", "ノワール", "cool", ("gothic", "mincho", "shippori", "dela"), number=5)
def noir(s):
    photo = fx.grayscale(fx.cover(s.image, s.size, (0.5, 0.35)), contrast=1.25)
    canvas = fill(s.size, "#050505")
    canvas.alpha_composite(photo)
    # Darken from the left (where the text sits) and from the bottom.
    canvas.alpha_composite(
        _shade(s.size, [(0, 0, 0, 240), (0, 0, 0, 200), (0, 0, 0, 40)], 0, (0, 0.45, 1))
    )
    canvas.alpha_composite(
        _shade(s.size, [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 200)], 90, (0, 0.55, 1))
    )
    fx.grain(canvas, 0.07)
    s.write(canvas, (120, 140, 1120, 830), (244, 244, 242), 104, valign="bottom")
    if s.has_credit:
        s.hline(canvas, 120, 200, 885, (244, 244, 242, 220), 3)
        s.credits(
            canvas,
            (120, 950),
            (244, 244, 242),
            (160, 160, 158),
            name_px=42,
            handle_px=30,
            gap=52,
            width=900,
        )
    return canvas


def _shade(size, colors, angle, steps=None):
    """Gradient with per-stop alpha (RGBA stops)."""

    w, h = size
    rgb = fx.gradient(size, [c[:3] for c in colors], angle, steps=steps)
    alpha = fx.gradient(size, [(c[3],) * 3 for c in colors], angle, steps=steps)
    rgb.putalpha(alpha.getchannel("R"))
    return rgb


@layout("cinema", "シネマ", "cool", ("gothic", "mincho", "maru"), number=6)
def cinema(s):
    canvas = fill(s.size, "#000000")
    band = s.box((0, 140, 1920, 940))
    photo = fx.cover(s.image, (band[2] - band[0], band[3] - band[1]), (0.5, 0.4))
    # A teal-and-amber grade, darkened so subtitles stay legible.
    graded = fx.duotone(photo, (8, 22, 30), (255, 214, 170), mid=(70, 104, 112))
    photo = Image.blend(photo, graded, 0.55)
    photo = ImageEnhance.Brightness(photo).enhance(0.62)
    canvas.alpha_composite(photo, band[:2])
    canvas.alpha_composite(
        _shade(s.size, [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 170)], 90, (0, 0.5, 0.87))
    )
    fx.grain(canvas, 0.05)
    region = s.box((260, 560, 1660, 900))
    result = s.fit(region, 58)
    mask = s.text_mask(result, region, align="center", valign="bottom")
    canvas.alpha_composite(fx.glow(mask, (0, 0, 0), s.S(6), 2.2))
    paint(canvas, result, region, (255, 247, 214), align="center", valign="bottom")
    s.line(canvas, s.handle_text, (90, 92), 26, (150, 150, 150), kind="handle", width=800)
    s.line(
        canvas,
        s.date_text,
        (1830, 92),
        26,
        (150, 150, 150),
        kind="handle",
        anchor="right",
        width=600,
    )
    if s.name:
        s.line(
            canvas,
            "— " + s.name,
            (960, 1028),
            34,
            (225, 225, 225),
            anchor="center",
            width=1600,
        )
    return canvas


@layout("neon", "ネオン", "cool", ("gothic", "maru", "rocknroll", "dela", "code"), number=7)
def neon(s):
    canvas = fx.radial(s.size, (34, 12, 54), (5, 4, 10), center=(0.75, 0.5), radius=0.6)
    cx, cy, r = 1460, 520, 250
    ring_box = (cx - r - 30, cy - r - 30, cx + r + 30, cy + r + 30)
    s.place(canvas, s.image, (cx - r, cy - r, cx + r, cy + r), circle=True, cover=True)
    ring = Image.new("L", s.size)
    l, t, rr, b = s.box(ring_box)
    side = min(rr - l, b - t)
    width = max(2, round(s.S(9)))
    outer = fx.ellipse_mask((side, side))
    inner = fx.ellipse_mask((side - 2 * width, side - 2 * width))
    hole = Image.new("L", (side, side))
    hole.paste(inner, (width, width))
    ring.paste(
        ImageChops.subtract(outer, hole),
        (round(s.X(cx) - side / 2), round(s.Y(cy) - side / 2)),
    )
    canvas.alpha_composite(fx.glow(ring, (255, 43, 214), s.S(26), 2.4))
    canvas.alpha_composite(fx.colored(ring, (255, 170, 240)))
    region = s.box((130, 170, 1100, 830))
    result = s.fit(region, 92)
    mask = s.text_mask(result, region)
    canvas.alpha_composite(fx.glow(mask, (0, 229, 255), s.S(30), 1.5))
    canvas.alpha_composite(fx.glow(mask, (0, 229, 255), s.S(8), 1.4))
    paint(canvas, result, region, (240, 253, 255))
    if s.name:
        name_layer = Image.new("RGBA", s.size)
        s.line(name_layer, s.name, (130, 945), 46, (255, 120, 230), width=900)
        canvas.alpha_composite(fx.glow(name_layer.getchannel("A"), (255, 43, 214), s.S(16), 1.8))
        canvas.alpha_composite(name_layer)
    s.line(
        canvas,
        s.handle_text,
        (130, 945 + (58 if s.name else 0)),
        30,
        (150, 220, 235),
        kind="handle",
        width=900,
    )
    return canvas


@layout("glitch", "グリッチ", "cool", ("code", "dot", "gothic", "dela"), number=8)
def glitch(s):
    canvas = fill(s.size, "#0a0a0f")
    l, t, r, b = s.box((1180, 150, 1790, 760))
    photo = fx.fitted(s.image, (r - l, b - t), s.attachment)
    photo = ImageEnhance.Contrast(fx.grayscale(photo)).enhance(1.3)
    photo = Image.blend(photo, fx.duotone(photo, (10, 0, 40), (120, 255, 240)), 0.6)
    photo = fx.rgb_split(photo, s.S(12))
    # Horizontal slices displaced like a damaged signal (seeded by the text).
    sliced = photo.copy()
    for _ in range(7):
        y0 = s.rng.randrange(0, max(1, photo.height - 4))
        hh = s.rng.randrange(max(2, photo.height // 40), max(3, photo.height // 9))
        dx = round(s.rng.choice((-1, 1)) * s.rng.uniform(0.02, 0.09) * photo.width)
        strip = photo.crop((0, y0, photo.width, min(photo.height, y0 + hh)))
        sliced.paste(ImageChops.offset(strip, dx, 0), (0, y0))
    x = l + (r - l - sliced.width) // 2
    y = t + (b - t - sliced.height) // 2
    canvas.alpha_composite(sliced, (x, y))
    for color in ((255, 0, 80, 255), (0, 240, 255, 255)):
        bx = s.rng.uniform(1100, 1700)
        by = s.rng.uniform(120, 820)
        s.rect(canvas, (bx, by, bx + s.rng.uniform(120, 260), by + 10), color)
    region = s.box((130, 160, 1060, 820))
    result = s.fit(region, 86)
    mask = s.text_mask(result, region)
    shift = max(1, round(s.S(5)))
    canvas.alpha_composite(fx.colored(ImageChops.offset(mask, -shift, 0), (255, 0, 80, 230)))
    canvas.alpha_composite(fx.colored(ImageChops.offset(mask, shift, 0), (0, 240, 255, 230)))
    paint(canvas, result, region, (245, 245, 250))
    fx.scanlines(canvas, s.S(6), 0.22)
    s.credits(
        canvas,
        (130, 940),
        (245, 245, 250),
        (0, 240, 255),
        prefix="> ",
        name_px=40,
        handle_px=30,
        gap=54,
        width=900,
    )
    return canvas


@layout("terminal", "ターミナル", "cool", ("code", "dot"), number=9)
def terminal(s):
    canvas = fx.gradient(s.size, [(28, 32, 44), (12, 14, 20)], 60)
    window = (110, 80, 1810, 1000)
    l, t, r, b = s.box(window)
    fx.shadow(
        canvas,
        fx.round_mask((r - l, b - t), s.S(22)),
        (l, t),
        blur=s.S(40),
        opacity=0.6,
        offset=(0, s.S(20)),
    )
    s.rect(canvas, window, (13, 17, 23), radius=22, outline=(48, 54, 61), width=2)
    s.rect(canvas, (112, 82, 1808, 150), (22, 27, 34), radius=20)
    s.rect(canvas, (112, 130, 1808, 150), (22, 27, 34))
    for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        x = 160 + i * 44
        s.vector(
            canvas,
            color,
            lambda d, P, S, x=x: d.ellipse((*P(x - 13, 116 - 13), *P(x + 13, 116 + 13))),
            (x - 16, 100, x + 16, 132),
        )
    s.line(
        canvas,
        "quote — zsh",
        (960, 128),
        26,
        (139, 148, 158),
        kind="handle",
        anchor="center",
        width=600,
    )
    who = s.handle or (s.name or "guest")
    y = 222
    prompt = f"{who}@miq"
    s.line(canvas, prompt, (180, y), 30, (63, 185, 80), kind="handle", width=600)
    offset = s.handle_font(max(1, round(30 * s.k))).getlength(prompt + " ") / s.kx
    s.line(
        canvas,
        "~ $ quote --render",
        (180 + offset, y),
        30,
        (201, 209, 217),
        kind="handle",
        width=800,
    )
    art = (1400, 210, 1730, 540)
    al, at, ar, ab = s.box(art)
    pic = fx.pixelate(fx.fitted(s.image, (ar - al, ab - at), s.attachment), 48, 6)
    pic = fx.duotone(pic, (6, 20, 10), (120, 255, 150))
    canvas.alpha_composite(pic, (al + (ar - al - pic.width) // 2, at + (ab - at - pic.height) // 2))
    s.frame(canvas, art, (48, 54, 61), 2)
    s.line(
        canvas,
        "avatar.png",
        (1565, 585),
        24,
        (139, 148, 158),
        kind="handle",
        anchor="center",
        width=330,
    )
    s.write(canvas, (180, 270, 1320, 820), (230, 237, 243), 84, valign="top")
    if s.name:
        s.line(canvas, "# — " + s.name, (180, 885), 32, (139, 148, 158), width=1100)
    s.line(canvas, "~ $", (180, 950), 30, (63, 185, 80), kind="handle")
    s.rect(canvas, (250, 922, 270, 958), (230, 237, 243))
    return canvas


@layout("swiss", "スイス", "cool", ("gothic", "dela"), number=10)
def swiss(s):
    canvas = fill(s.size, "#f2efe8")
    red = (226, 35, 26)
    s.rect(canvas, (0, 0, 700, 700), red)
    l, t, r, b = s.box((110, 110, 590, 590))
    photo = fx.fitted(s.image, (r - l, b - t), s.attachment)
    photo = fx.duotone(photo, (20, 10, 10), (255, 236, 230), mid=red)
    canvas.alpha_composite(photo, (l + (r - l - photo.width) // 2, t + (b - t - photo.height) // 2))
    s.hline(canvas, 0, 1920, 700, (20, 20, 20), 3)
    s.vline(canvas, 700, 0, 1080, (20, 20, 20), 3)
    s.hline(canvas, 700, 1920, 120, (20, 20, 20), 1)
    s.write(canvas, (800, 160, 1810, 650), (17, 17, 17), 92, valign="top")
    s.line(canvas, "QUOTE", (110, 820), 54, (17, 17, 17))
    s.line(canvas, s.date_text, (110, 880), 30, (17, 17, 17), kind="handle")
    s.line(
        canvas,
        "№ " + str(zlib.crc32(s.text.encode()) % 900 + 100),
        (110, 990),
        30,
        red,
        kind="handle",
    )
    s.credits(
        canvas,
        (800, 860),
        (17, 17, 17),
        (110, 110, 110),
        name_px=56,
        handle_px=32,
        gap=66,
        width=1000,
    )
    return canvas


@layout("brutal", "ブルータル", "cool", ("dela", "gothic", "rocknroll"), number=11)
def brutal(s):
    canvas = fill(s.size, "#ffd93d")
    dots = Image.new("L", s.size)
    draw = ImageDraw.Draw(dots)
    step = max(6, round(s.S(44)))
    rad = max(1, s.S(3))
    for y in range(step // 2, s.h, step):
        for x in range(step // 2, s.w, step):
            draw.ellipse((x - rad, y - rad, x + rad, y + rad), fill=60)
    canvas.alpha_composite(fx.colored(dots, (0, 0, 0)))
    ink = (17, 17, 17)
    card = (110, 100, 1250, 960)
    s.rect(canvas, (card[0] + 22, card[1] + 22, card[2] + 22, card[3] + 22), ink, radius=18)
    s.rect(canvas, card, (255, 255, 255), radius=18, outline=ink, width=8)
    s.write(canvas, (190, 180, 1170, 880), ink, 90)
    pic = (1360, 150, 1790, 580)
    s.rect(canvas, (pic[0] + 20, pic[1] + 20, pic[2] + 20, pic[3] + 20), ink, radius=18)
    s.rect(canvas, pic, (255, 107, 203), radius=18, outline=ink, width=8)
    s.place(canvas, s.image, (pic[0] + 8, pic[1] + 8, pic[2] - 8, pic[3] - 8), radius=10)
    if s.name:
        s.rect(canvas, (1380, 680, 1810, 680 + 100), ink, radius=50)
        s.rect(
            canvas,
            (1360, 660, 1790, 760),
            (255, 107, 203),
            radius=50,
            outline=ink,
            width=6,
        )
        s.line(canvas, s.name, (1575, 728), 42, ink, anchor="center", width=380)
    s.line(
        canvas,
        s.handle_text,
        (1575, 840 if s.name else 720),
        32,
        ink,
        kind="handle",
        anchor="center",
        width=430,
    )
    s.vector(
        canvas,
        ink,
        lambda d, P, S: fx.star(d, *P(1230, 110), S(70), 255, 4, 0.3),
        (1150, 30, 1310, 190),
    )
    s.vector(
        canvas,
        (255, 255, 255),
        lambda d, P, S: fx.star(d, *P(1230, 110), S(48), 255, 4, 0.3),
        (1150, 30, 1310, 190),
    )
    return canvas


DUOTONES = (
    ((29, 26, 77), (255, 90, 95), (243, 239, 230)),
    ((14, 34, 64), (62, 242, 200), (236, 242, 238)),
    ((61, 12, 17), (255, 182, 39), (246, 238, 224)),
    ((16, 42, 18), (198, 255, 74), (240, 243, 230)),
    ((40, 14, 70), (255, 130, 220), (245, 236, 246)),
    ((20, 20, 24), (90, 160, 255), (236, 239, 245)),
)


@layout(
    "duotone",
    "デュオトーン",
    "cool",
    ("dela", "gothic", "mincho", "shippori"),
    number=12,
)
def duotone(s):
    hue = fx.accent(s.image)
    # Pick the palette nearest the photo's own accent colour.
    dark, light, paper = min(
        DUOTONES,
        key=lambda p: sum((a - b) ** 2 for a, b in zip(p[1], hue)),
    )
    canvas = fill(s.size, paper)
    photo_box = s.box((90, 90, 900, 990))
    l, t, r, b = photo_box
    photo = fx.duotone(fx.fitted(s.image, (r - l, b - t), s.attachment), dark, light)
    x, y = l + (r - l - photo.width) // 2, t + (b - t - photo.height) // 2
    canvas.alpha_composite(photo, (x, y))
    over = Image.new("L", s.size)
    over.paste(photo.getchannel("A").point(lambda v: 255 if v > 128 else 0), (x, y))
    region = s.box((680, 150, 1820, 830))
    result = s.fit(region, 112)
    mask = s.text_mask(result, region)
    canvas.alpha_composite(fx.colored(ImageChops.multiply(mask, ImageOps.invert(over)), dark))
    canvas.alpha_composite(fx.colored(ImageChops.multiply(mask, over), paper))
    s.pictures(canvas, result, region)
    s.hline(canvas, 1000, 1100, 900, dark, 6)
    s.credits(
        canvas,
        (1000, 975),
        dark,
        fx.mix(dark, paper, 0.45),
        name_px=44,
        handle_px=30,
        gap=0 if not s.name else 52,
        width=820,
    )
    return canvas


@layout("aurora", "オーロラ", "cool", ("gothic", "maru", "mincho", "shippori"), number=13)
def aurora(s):
    canvas = fill(s.size, "#060914")
    w, h = s.size
    canvas.alpha_composite(
        fx.blobs(
            s.size,
            [
                (s.X(300), s.Y(150), s.X(760), s.Y(420), (40, 230, 200, 190)),
                (s.X(1550), s.Y(220), s.X(620), s.Y(420), (130, 60, 255, 200)),
                (s.X(1050), s.Y(980), s.X(820), s.Y(330), (255, 70, 150, 150)),
                (s.X(1800), s.Y(900), s.X(420), s.Y(300), (0, 150, 255, 170)),
            ],
            s.S(240),
        )
    )
    fx.grain(canvas, 0.035)
    card = (150, 130, 1770, 950)
    l, t, r, b = s.box(card)
    mask = fx.round_mask((r - l, b - t), s.S(48))
    fx.shadow(canvas, mask, (l, t), blur=s.S(50), opacity=0.45, offset=(0, s.S(24)))
    # Frosted glass: the card shows a stronger blur of what is behind it.
    behind = fx.soft_blur(canvas.crop((l, t, r, b)), s.S(40))
    behind = Image.blend(behind, Image.new("RGBA", behind.size, (255, 255, 255, 255)), 0.12)
    canvas.paste(behind, (l, t), mask)
    s.frame(canvas, card, (255, 255, 255, 90), 2, radius=48)
    s.place(canvas, s.image, (240, 220, 400, 380), circle=True, cover=True)
    if s.has_credit:
        s.credits(
            canvas,
            (440, 290),
            (255, 255, 255),
            (200, 205, 230),
            name_px=44,
            handle_px=30,
            gap=50,
            width=1200,
        )
    s.write(canvas, (240, 440, 1680, 870), (255, 255, 255), 80)
    return canvas


@layout("blueprint", "ブループリント", "cool", ("code", "gothic"), number=14)
def blueprint(s):
    canvas = fx.radial(s.size, (22, 84, 176), (10, 52, 128), center=(0.5, 0.45), radius=0.9)
    grid = Image.new("L", s.size)
    draw = ImageDraw.Draw(grid)
    minor = s.S(40)
    i = 0
    x = 0.0
    while x < s.w:
        draw.line((round(x), 0, round(x), s.h), fill=70 if i % 5 == 0 else 28)
        x += minor
        i += 1
    i = 0
    y = 0.0
    while y < s.h:
        draw.line((0, round(y), s.w, round(y)), fill=70 if i % 5 == 0 else 28)
        y += minor
        i += 1
    canvas.alpha_composite(fx.colored(grid, (200, 225, 255)))
    white = (236, 244, 255)
    art = (1240, 160, 1720, 640)
    l, t, r, b = s.box(art)
    pic = fx.fitted(s.image, (r - l, b - t), s.attachment)
    lines = fx.line_art(pic, white, 3.2)
    canvas.alpha_composite(lines, (l + (r - l - pic.width) // 2, t + (b - t - pic.height) // 2))
    s.frame(canvas, art, white + (200,), 2)
    # Dimension line under the figure.
    s.hline(canvas, 1240, 1720, 690, white + (200,), 2)
    for x in (1240, 1720):
        s.vline(canvas, x, 670, 710, white + (200,), 2)
    s.line(
        canvas,
        f"FIG.1  {s.image.width}×{s.image.height}",
        (1480, 740),
        24,
        white,
        kind="handle",
        anchor="center",
        width=480,
    )
    s.write(canvas, (160, 170, 1110, 900), white, 76)
    table = (1240, 800, 1780, 980)
    s.frame(canvas, table, white + (220,), 2)
    s.hline(canvas, 1240, 1780, 860, white + (160,), 2)
    s.hline(canvas, 1240, 1780, 920, white + (160,), 2)
    s.vline(canvas, 1360, 800, 980, white + (160,), 2)
    for row, (label, value) in enumerate(
        (("NAME", s.name or "—"), ("ID", s.handle_text or "—"), ("DATE", s.date_text))
    ):
        y = 842 + row * 60
        s.line(canvas, label, (1260, y), 22, (170, 200, 240), kind="handle", width=100)
        s.line(canvas, value, (1380, y), 28, white, width=390)
    return canvas


@layout("manga", "マンガ", "cool", ("gothic", "dela", "rocknroll", "maru"), number=15)
def manga(s):
    canvas = fill(s.size, "#fbfbf8")
    ink = (17, 17, 17)
    left = (70, 70, 820, 1010)
    l, t, r, b = s.box(left)
    photo = fx.cover(s.image, (r - l, b - t), (0.5, 0.4))
    canvas.alpha_composite(fx.halftone(photo, s.S(11), ink=ink, paper=(251, 251, 248)), (l, t))
    s.frame(canvas, left, ink, 9)
    panel = (870, 70, 1850, 1010)
    pl, pt, pr, pb = s.box(panel)
    cx, cy = (pl + pr) / 2, (pt + pb) / 2 - s.Y(20)
    lines = Image.new("L", (pr - pl, pb - pt))
    draw = ImageDraw.Draw(lines)
    radius = math.hypot(pr - pl, pb - pt)
    for i in range(140):
        angle = s.rng.uniform(0, math.tau)
        inner = s.rng.uniform(0.42, 0.5) * min(pr - pl, pb - pt)
        spread = s.rng.uniform(0.002, 0.008)
        ox, oy = cx - pl, cy - pt
        draw.polygon(
            [
                (ox + math.cos(angle) * inner, oy + math.sin(angle) * inner * 0.8),
                (
                    ox + math.cos(angle - spread) * radius,
                    oy + math.sin(angle - spread) * radius,
                ),
                (
                    ox + math.cos(angle + spread) * radius,
                    oy + math.sin(angle + spread) * radius,
                ),
            ],
            fill=255,
        )
    canvas.alpha_composite(fx.colored(lines, ink), (pl, pt))
    s.frame(canvas, panel, ink, 9)
    bubble = (930, 170, 1790, 880)

    def bubble_shape(d, P, S, grow=0):
        d.ellipse(
            (
                *P(bubble[0] - grow, bubble[1] - grow),
                *P(bubble[2] + grow, bubble[3] + grow),
            ),
            fill=255,
        )
        d.polygon([P(1000 - grow, 700), P(820 - grow * 2, 900), P(1110 + grow, 780)], fill=255)

    s.vector(canvas, ink, lambda d, P, S: bubble_shape(d, P, S, 7), (790, 150, 1810, 930))
    s.vector(
        canvas,
        (255, 255, 255),
        lambda d, P, S: bubble_shape(d, P, S, 0),
        (790, 150, 1810, 930),
    )
    rx, ry = (bubble[2] - bubble[0]) / 2, (bubble[3] - bubble[1]) / 2
    mx, my = (bubble[0] + bubble[2]) / 2, (bubble[1] + bubble[3]) / 2
    s.write(
        canvas,
        (mx - rx * 0.7, my - ry * 0.62, mx + rx * 0.7, my + ry * 0.62),
        ink,
        84,
        align="center",
    )
    if s.has_credit:
        label = s.name or s.handle_text
        font = s.name_font(max(1, round(36 * s.k * s.family.scale)))
        width = min(700, font.getlength(label) / s.kx + 60)
        s.rect(canvas, (900, 100, 900 + width, 170), (255, 255, 255), outline=ink, width=5)
        s.line(canvas, label, (930, 148), 36, ink, width=width - 60)
    return canvas


@layout("tategaki", "縦書き", "cool", ("shippori", "mincho", "klee"), number=16)
def tategaki(s):
    from .miq_vertical import draw_vertical, suits_vertical

    paper = (244, 238, 226)
    canvas = fill(s.size, paper)
    fibres = fx.soft_blur(fx.noise(s.size, 70), s.S(1.2))
    canvas.alpha_composite(fx.colored(fibres.point(lambda v: max(0, v - 150)), (160, 130, 90)))
    ink = (28, 24, 22)
    vermilion = (192, 52, 36)
    frame = (110, 110, 470, 470)
    s.place(canvas, s.image, frame)
    s.frame(canvas, frame, (60, 50, 40), 2)
    if s.name:
        s.line(canvas, s.name, (290, 560), 40, ink, anchor="center", width=360)
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (290, 560 + (56 if s.name else 0)),
            26,
            (120, 110, 100),
            kind="handle",
            anchor="center",
            width=360,
        )
    if s.name:
        seal = (230, 760, 350, 880)
        s.rect(canvas, seal, vermilion, radius=14)
        s.frame(canvas, (238, 768, 342, 872), paper, 3, radius=10)
        first = next(iter(_graphemes(s.name)), "")
        s.line(canvas, first, (290, 848), 66, paper, anchor="center", width=100)
    region = s.box((590, 110, 1800, 970))
    if s.aa:
        s.write(canvas, (590, 140, 1780, 940), ink, 84)
    elif suits_vertical(s.text):
        draw_vertical(canvas, s, region, ink, max_px=100 * s.k * s.family.scale)
    else:
        s.write(canvas, (590, 140, 1780, 940), ink, 84)
    s.vline(canvas, 530, 110, 970, (120, 100, 80, 120), 1)
    return canvas


def _graphemes(text):
    from .miq_words import GRAPHEME

    return GRAPHEME.findall(text)


GOLD = ((176, 128, 52), (247, 226, 164), (196, 150, 70), (240, 214, 140))


@layout("luxe", "ラグジュアリー", "cool", ("shippori", "mincho"), number=17)
def luxe(s):
    canvas = fx.radial(s.size, (22, 40, 33), (7, 13, 11), center=(0.5, 0.42), radius=0.8)
    gold = fx.gradient(s.size, GOLD, 70)
    frame = Image.new("L", s.size)
    for inset, width in ((48, 3), (66, 1.5)):
        l, t, r, b = s.box((inset, inset, 1920 - inset, 1080 - inset))
        stroke = max(1, round(s.S(width)))
        ImageDraw.Draw(frame).rectangle((l, t, r, b), outline=255, width=stroke)
    for x, y in ((57, 57), (1863, 57), (57, 1023), (1863, 1023)):
        dl, dt, dr, db = s.box((x - 16, y - 16, x + 16, y + 16))
        cxp, cyp = (dl + dr) / 2, (dt + db) / 2
        rr = s.S(14)
        ImageDraw.Draw(frame).polygon(
            [(cxp, cyp - rr), (cxp + rr, cyp), (cxp, cyp + rr), (cxp - rr, cyp)],
            fill=255,
        )
    canvas.alpha_composite(Image.composite(gold, Image.new("RGBA", s.size), frame))
    cx, r = 960, 115
    ring = (cx - r - 10, 150 - 10, cx + r + 10, 150 + 2 * r + 10)
    l, t, rr, b = s.box(ring)
    side = min(rr - l, b - t)
    disc = Image.new("L", s.size)
    disc.paste(fx.ellipse_mask((side, side)), (round(s.X(cx) - side / 2), (t + b - side) // 2))
    canvas.alpha_composite(Image.composite(gold, Image.new("RGBA", s.size), disc))
    s.place(canvas, s.image, (cx - r, 150, cx + r, 150 + 2 * r), circle=True, cover=True)
    region = s.box((260, 430, 1660, 850))
    result = s.fit(region, 84)
    mask = s.text_mask(result, region, align="center")
    canvas.alpha_composite(fx.glow(mask, (0, 0, 0), s.S(10), 1.2))
    canvas.alpha_composite(Image.composite(gold, Image.new("RGBA", s.size), mask))
    s.pictures(canvas, result, region, align="center")
    if s.name:
        s.line(canvas, s.name, (960, 935), 40, (226, 196, 120), anchor="center", width=1000)
        font = s.name_font(max(1, round(40 * s.k * s.family.scale)))
        half = min(500, font.getlength(s.name) / s.kx / 2) + 40
        s.hline(canvas, 960 - half - 120, 960 - half, 922, (200, 160, 80), 2)
        s.hline(canvas, 960 + half, 960 + half + 120, 922, (200, 160, 80), 2)
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (960, 990 if s.name else 935),
            24,
            (150, 140, 110),
            kind="handle",
            anchor="center",
            width=900,
        )
    return canvas


@layout("newspaper", "新聞", "cool", ("mincho", "shippori", "gothic"), number=18)
def newspaper(s):
    paper = (238, 232, 218)
    ink = (26, 24, 22)
    canvas = fill(s.size, paper)
    fx.grain(canvas, 0.05)
    s.line(canvas, "名 言 新 聞", (960, 128), 84, ink, anchor="center", width=1700)
    s.hline(canvas, 80, 1840, 168, ink, 5)
    s.hline(canvas, 80, 1840, 180, ink, 1.5)
    number = zlib.crc32(s.text.encode()) % 9000 + 1000
    s.line(canvas, f"第{number}号", (90, 222), 26, ink, kind="handle", width=500)
    s.line(
        canvas,
        s.date_text,
        (960, 222),
        26,
        ink,
        kind="handle",
        anchor="center",
        width=500,
    )
    s.line(
        canvas,
        s.handle_text,
        (1830, 222),
        26,
        ink,
        kind="handle",
        anchor="right",
        width=500,
    )
    s.hline(canvas, 80, 1840, 246, ink, 1.5)
    s.vline(canvas, 1210, 280, 1000, ink, 1.5)
    s.write(canvas, (90, 290, 1170, 1000), ink, 104, valign="top")
    photo = (1250, 290, 1840, 860)
    l, t, r, b = s.box(photo)
    pic = fx.fitted(s.image, (r - l, b - t), s.attachment)
    pic = fx.halftone(pic, s.S(7), ink=ink, paper=paper, angle=45)
    canvas.alpha_composite(pic, (l + (r - l - pic.width) // 2, t))
    if s.name:
        s.line(canvas, "写真：" + s.name, (1250, 905), 26, ink, width=590)
    s.hline(canvas, 1250, 1840, 940, ink, 1)
    s.line(
        canvas,
        "本日の一言を掲載",
        (1250, 985),
        24,
        (90, 86, 80),
        kind="handle",
        width=590,
    )
    return canvas


@layout("ticket", "チケット", "cool", ("code", "gothic", "dela"), number=19)
def ticket(s):
    accent = fx.accent(s.image)
    canvas = fx.gradient(s.size, [fx.shade(accent, value=0.32), fx.shade(accent, value=0.16)], 120)
    paper = (247, 242, 232)
    ink = (28, 26, 30)
    card = (120, 190, 1800, 890)
    l, t, r, b = s.box(card)
    mask = fx.round_mask((r - l, b - t), s.S(24))
    stub = s.X(1430) - l
    notch = round(s.S(30))
    cut = ImageDraw.Draw(mask)
    for y in (0, b - t):
        cut.ellipse((stub - notch, y - notch, stub + notch, y + notch), fill=0)
    fx.shadow(canvas, mask, (l, t), blur=s.S(30), opacity=0.5, offset=(0, s.S(18)))
    canvas.alpha_composite(fx.colored(mask, paper), (l, t))
    band = Image.new("L", mask.size)
    band.paste(mask.crop((0, 0, round(s.S(26)), mask.height)), (0, 0))
    canvas.alpha_composite(fx.colored(band, accent), (l, t))
    y = 230
    while y < 860:
        s.vline(canvas, 1430, y, y + 18, (170, 160, 150), 3)
        y += 34
    s.line(canvas, "ADMIT ONE", (200, 270), 28, accent, kind="handle", width=600)
    s.line(
        canvas,
        "No." + str(zlib.crc32(s.text.encode()) % 900000 + 100000),
        (1360, 270),
        26,
        (120, 110, 100),
        kind="handle",
        anchor="right",
        width=500,
    )
    s.write(canvas, (200, 310, 1360, 730), ink, 80)
    s.hline(canvas, 200, 1360, 770, (200, 190, 180), 2)
    for i, (label, value) in enumerate(
        (
            ("GUEST", s.name or s.handle_text or "—"),
            ("DATE", s.date_text),
            (
                "SEAT",
                f"{chr(65 + zlib.crc32(s.text.encode()) % 8)}-{zlib.crc32(s.name.encode()) % 40 + 1}",
            ),
        )
    ):
        x = 200 + i * 400
        s.line(canvas, label, (x, 812), 20, (140, 130, 120), kind="handle", width=360)
        s.line(canvas, value, (x, 856), 32, ink, width=360 if i else 380)
    s.place(canvas, s.image, (1505, 260, 1725, 480), circle=True, cover=True)
    s.line(
        canvas,
        s.handle_text or s.name,
        (1615, 540),
        26,
        ink,
        kind="handle",
        anchor="center",
        width=300,
    )
    seed = zlib.crc32((s.text + s.name).encode())
    x = 1480.0
    bars = Image.new("L", s.size)
    draw = ImageDraw.Draw(bars)
    while x < 1750:
        width = 3 + (seed % 3) * 3
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        draw.rectangle(
            (
                round(s.X(x)),
                round(s.Y(620)),
                max(round(s.X(x)), round(s.X(x + width)) - 1),
                round(s.Y(820)),
            ),
            fill=255,
        )
        x += width + 4 + (seed % 3) * 3
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    canvas.alpha_composite(fx.colored(bars, ink))
    return canvas


@layout("nowplaying", "再生中", "cool", ("gothic", "maru"), number=20)
def nowplaying(s):
    canvas = fx.backdrop(s.image, s.size, s.S(90), darken=0.45, saturation=1.35)
    fx.grain(canvas, 0.03)
    art = (150, 190, 810, 850)
    l, t, r, b = s.box(art)
    fx.shadow(
        canvas,
        fx.round_mask((r - l, b - t), s.S(28)),
        (l, t),
        blur=s.S(40),
        opacity=0.55,
        offset=(0, s.S(22)),
    )
    s.place(canvas, s.image, art, radius=28)
    white = (255, 255, 255)
    soft = (255, 255, 255, 170)
    s.line(canvas, "NOW PLAYING", (930, 230), 24, soft, kind="handle", width=600)
    title = s.name or s.handle_text or "Unknown"
    s.line(canvas, title, (930, 300), 56, white, width=860)
    if s.name and s.handle:
        s.line(canvas, s.handle_text, (930, 356), 30, soft, kind="handle", width=860)
    s.write(canvas, (930, 400, 1790, 770), white, 64)
    progress = 0.25 + (zlib.crc32(s.text.encode()) % 50) / 100
    s.rect(canvas, (930, 826, 1790, 834), (255, 255, 255, 70), radius=4)
    end = 930 + 860 * progress
    s.rect(canvas, (930, 826, end, 834), white, radius=4)
    s.vector(
        canvas,
        white,
        lambda d, P, S: d.ellipse((*P(end - 13, 830 - 13), *P(end + 13, 830 + 13))),
        (end - 20, 810, end + 20, 850),
    )
    total = 150 + len(s.text) % 120
    played = round(total * progress)
    s.line(canvas, f"{played // 60}:{played % 60:02d}", (930, 885), 22, soft, kind="handle")
    s.line(
        canvas,
        f"-{(total - played) // 60}:{(total - played) % 60:02d}",
        (1790, 885),
        22,
        soft,
        kind="handle",
        anchor="right",
    )
    cx, cy = 1360, 965

    def controls(d, P, S):
        d.ellipse((*P(cx - 46, cy - 46), *P(cx + 46, cy + 46)), fill=255)
        for sign in (-1, 1):
            x = cx + sign * 170
            d.polygon(
                [
                    P(x - sign * 22, cy - 22),
                    P(x + sign * 4, cy),
                    P(x - sign * 22, cy + 22),
                ],
                fill=255,
            )
            d.polygon(
                [
                    P(x + sign * 4, cy - 22),
                    P(x + sign * 30, cy),
                    P(x + sign * 4, cy + 22),
                ],
                fill=255,
            )
            d.rectangle(
                (*P(x + sign * 30 - 4, cy - 22), *P(x + sign * 30 + 4, cy + 22)),
                fill=255,
            )

    s.vector(canvas, white, controls, (cx - 240, cy - 60, cx + 240, cy + 60))
    s.vector(
        canvas,
        (30, 30, 40),
        lambda d, P, S: (
            d.rectangle((*P(cx - 15, cy - 20), *P(cx - 5, cy + 20)), fill=255),
            d.rectangle((*P(cx + 5, cy - 20), *P(cx + 15, cy + 20)), fill=255),
        ),
        (cx - 30, cy - 30, cx + 30, cy + 30),
    )
    return canvas


@layout("diagonal", "ダイアゴナル", "cool", ("gothic", "dela", "mincho"), number=21)
def diagonal(s):
    accent = fx.accent(s.image, value=(0.3, 0.75))
    canvas = fill(s.size, accent)
    ink = fx.ink_for(accent)
    photo = fx.cover(s.image, s.size, (0.7, 0.4))
    shape = [(1060, 0), (1920, 0), (1920, 1080), (780, 1080)]
    mask = Image.new("RGBA", s.size)
    s.vector(
        mask,
        (255, 255, 255),
        lambda d, P, S: d.polygon([P(*p) for p in shape], fill=255),
    )
    edge = [(1000, 0), (1030, 0), (750, 1080), (720, 1080)]
    canvas.paste(photo, (0, 0), mask.getchannel("A"))
    s.vector(canvas, ink + (230,), lambda d, P, S: d.polygon([P(*p) for p in edge], fill=255))
    s.write(canvas, (110, 140, 800, 820), ink, 96)
    s.credits(
        canvas,
        (110, 930),
        ink,
        fx.mix(ink, accent, 0.4),
        name_px=44,
        handle_px=30,
        gap=52,
        width=640,
    )
    return canvas


@layout("gallery", "ギャラリー", "cool", ("mincho", "shippori", "gothic"), number=22)
def gallery(s):
    wall = fx.radial(s.size, (250, 249, 245), (226, 223, 216), center=(0.3, 0.05), radius=1.0)
    canvas = wall
    # Picture: a black frame and white mat sized to the artwork.
    area = s.box((170, 150, 860, 900))
    aw, ah = area[2] - area[0], area[3] - area[1]
    mat = round(s.S(54))
    border = max(2, round(s.S(14)))
    inner = (aw - 2 * (mat + border), ah - 2 * (mat + border))
    pic = (
        fx.fitted(s.image, inner, s.attachment)
        if s.attachment
        else fx.cover(s.image, (min(inner), min(inner)))
    )
    fw, fh = pic.width + 2 * (mat + border), pic.height + 2 * (mat + border)
    fx0 = area[0] + (aw - fw) // 2
    fy0 = area[1] + (ah - fh) // 2
    light = fx.blobs(
        s.size,
        [(fx0 + fw / 2, fy0 - s.S(40), fw * 0.7, fh * 0.6, (255, 250, 235, 120))],
        s.S(160),
    )
    canvas.alpha_composite(light)
    fx.shadow(
        canvas,
        Image.new("L", (fw, fh), 255),
        (fx0, fy0),
        blur=s.S(22),
        opacity=0.35,
        offset=(s.S(10), s.S(22)),
    )
    canvas.alpha_composite(fill((fw, fh), (24, 22, 20)), (fx0, fy0))
    canvas.alpha_composite(
        fill((fw - 2 * border, fh - 2 * border), (250, 249, 246)),
        (fx0 + border, fy0 + border),
    )
    canvas.alpha_composite(pic, (fx0 + border + mat, fy0 + border + mat))
    s.write(canvas, (1000, 170, 1790, 740), (40, 38, 36), 72)
    plaque = (1000, 820, 1460, 960)
    l, t, r, b = s.box(plaque)
    fx.shadow(
        canvas,
        Image.new("L", (r - l, b - t), 255),
        (l, t),
        blur=s.S(6),
        opacity=0.25,
        offset=(0, s.S(4)),
    )
    s.rect(canvas, plaque, (253, 252, 250))
    s.line(
        canvas,
        s.name or s.handle_text or "Untitled",
        (1030, 875),
        32,
        (30, 28, 26),
        width=400,
    )
    caption = ", ".join(filter(None, [s.handle_text if s.name else "", str(s.date.year)]))
    s.line(canvas, caption, (1030, 925), 22, (120, 116, 110), kind="handle", width=400)
    return canvas


# ---------------------------------------------------------------------------
# Cute
# ---------------------------------------------------------------------------


@layout("kawaii", "パステル", "cute", ("maru", "rocknroll", "klee"), number=23)
def kawaii(s):
    canvas = fx.gradient(s.size, [(255, 214, 232), (228, 214, 255), (205, 239, 253)], 25)
    dots = Image.new("L", s.size)
    draw = ImageDraw.Draw(dots)
    step = s.S(64)
    rad = s.S(6)
    row = 0
    y = step / 2
    while y < s.h + step:
        x = step / 2 + (step / 2 if row % 2 else 0)
        while x < s.w + step:
            draw.ellipse((x - rad, y - rad, x + rad, y + rad), fill=110)
            x += step
        y += step
        row += 1
    canvas.alpha_composite(fx.colored(dots, (255, 255, 255)))
    cx, cy, r = 1440, 470, 230
    l, t, rr, b = s.box((cx - r - 18, cy - r - 18, cx + r + 18, cy + r + 18))
    side = min(rr - l, b - t)
    disc = fx.ellipse_mask((side, side))
    ox, oy = round(s.X(cx) - side / 2), round(s.Y(cy) - side / 2)
    fx.shadow(
        canvas,
        disc,
        (ox, oy),
        blur=s.S(26),
        color=(255, 120, 170),
        opacity=0.45,
        offset=(0, s.S(14)),
    )
    canvas.alpha_composite(fx.colored(disc, (255, 255, 255)), (ox, oy))
    s.place(canvas, s.image, (cx - r, cy - r, cx + r, cy + r), circle=True, cover=True)
    pink, yellow, mint = (255, 126, 182), (255, 214, 102), (126, 220, 200)
    deco = [
        (1170, 230, 46, pink, "heart"),
        (1720, 260, 38, yellow, "star"),
        (1730, 700, 52, pink, "star"),
        (1150, 690, 30, mint, "star"),
        (1640, 150, 28, (255, 255, 255), "star"),
        (1250, 120, 22, yellow, "heart"),
    ]
    for x, y, size, color, kind in deco:
        if kind == "heart":
            s.vector(
                canvas,
                color,
                lambda d, P, S, x=x, y=y, size=size: fx.heart(d, *P(x, y), S(size * 2), 255),
                (x - size * 2, y - size * 2, x + size * 2, y + size * 2),
            )
        else:
            s.vector(
                canvas,
                color,
                lambda d, P, S, x=x, y=y, size=size: fx.star(d, *P(x, y), S(size), 255, 4, 0.38),
                (x - size - 4, y - size - 4, x + size + 4, y + size + 4),
            )
    ink = (92, 58, 120)
    region = s.box((130, 150, 1080, 900))
    result = s.fit(region, 86)
    mask = s.text_mask(result, region)
    canvas.alpha_composite(fx.colored(fx.outline(mask, s.S(9)), (255, 255, 255)))
    paint(canvas, result, region, ink)
    if s.name:
        font = s.name_font(max(1, round(40 * s.k * s.family.scale)))
        half = min(260, font.getlength(s.name) / s.kx / 2 + 40)
        s.rect(canvas, (cx - half, 760, cx + half, 840), (255, 255, 255), radius=40)
        s.line(canvas, s.name, (cx, 815), 40, pink, anchor="center", width=half * 2 - 60)
    s.line(
        canvas,
        s.handle_text,
        (cx, 900 if s.name else 800),
        30,
        ink,
        kind="handle",
        anchor="center",
        width=600,
    )
    return canvas


@layout("chat", "チャット", "cute", ("maru", "gothic", "rocknroll"), number=24)
def chat(s):
    canvas = fx.gradient(s.size, [(220, 234, 248), (242, 236, 252)], 90)
    pad = 54
    inner_max = s.box((420, 0, 1660, 640))
    result = s.fit((0, 0, inner_max[2] - inner_max[0], inner_max[3]), 80)
    width = max((result.font.getlength(r) * result.scale for r in result.lines), default=0)
    height = result.height
    bw = width / s.kx + 2 * pad
    bh = height / s.ky + 2 * pad
    top = max(150, 540 - bh / 2 + 20)
    left = 400
    bubble = (left, top, left + bw, top + bh)
    l, t, r, b = s.box(bubble)
    mask = fx.round_mask((r - l, b - t), s.S(44))
    fx.shadow(
        canvas,
        mask,
        (l, t),
        blur=s.S(20),
        color=(90, 110, 160),
        opacity=0.18,
        offset=(0, s.S(10)),
    )
    s.vector(
        canvas,
        (255, 255, 255),
        lambda d, P, S: d.polygon(
            [P(left + 6, top + 40), P(left - 34, top + 18), P(left + 40, top + 90)],
            fill=255,
        ),
        (left - 50, top, left + 60, top + 100),
    )
    canvas.alpha_composite(fx.colored(mask, (255, 255, 255)), (l, t))
    region = (
        l + round(s.X(pad)),
        t + round(s.Y(pad)),
        r - round(s.X(pad)),
        b - round(s.Y(pad)),
    )
    paint(canvas, result, region, (40, 44, 60))
    s.place(canvas, s.image, (190, top - 10, 340, top + 140), circle=True, cover=True)
    if s.has_credit:
        s.line(
            canvas,
            s.name or s.handle_text,
            (420, top - 26),
            32,
            (90, 96, 120),
            width=1100,
        )
    meta = s.date.strftime("%H:%M")
    s.line(
        canvas,
        "既読",
        (left + bw + 20, top + bh - 46),
        24,
        (120, 126, 150),
        kind="handle",
        width=200,
    )
    s.line(
        canvas,
        meta,
        (left + bw + 20, top + bh - 12),
        24,
        (120, 126, 150),
        kind="handle",
        width=200,
    )
    # A reaction chip under the bubble.
    chip = (left + 30, top + bh - 18, left + 170, top + bh + 52)
    if chip[3] < 1060:
        s.rect(canvas, chip, (255, 255, 255), radius=35, outline=(222, 226, 240), width=3)
        s.vector(
            canvas,
            (255, 92, 138),
            lambda d, P, S: fx.heart(d, *P(left + 76, top + bh + 16), S(44), 255),
            (left + 40, top + bh - 15, left + 112, top + bh + 50),
        )
        s.line(canvas, "1", (left + 122, top + bh + 30), 26, (90, 96, 120), kind="handle")
    return canvas


@layout("notebook", "ノート", "cute", ("klee", "maru"), number=25)
def notebook(s):
    canvas = fill(s.size, (255, 253, 245))
    y = 150
    while y < 1080:
        s.hline(canvas, 0, 1920, y, (172, 206, 236), 2)
        y += 76
    s.vline(canvas, 210, 0, 1080, (255, 150, 160), 2.5)
    for y in (180, 540, 900):
        s.vector(
            canvas,
            (232, 226, 210),
            lambda d, P, S, y=y: d.ellipse((*P(95 - 22, y - 22), *P(95 + 22, y + 22)), fill=255),
            (60, y - 40, 130, y + 40),
        )
    # Taped photo, slightly turned.
    pic_box = s.box((1290, 160, 1750, 620))
    side = pic_box[2] - pic_box[0]
    border = round(s.S(18))
    photo = fx.fitted(s.image, (side, pic_box[3] - pic_box[1]), s.attachment)
    card = Image.new(
        "RGBA",
        (photo.width + border * 2, photo.height + border * 2 + round(s.S(40))),
        (255, 255, 255, 255),
    )
    card.alpha_composite(photo, (border, border))
    card = card.rotate(-4, resample=Image.Resampling.BICUBIC, expand=True)
    x, y = pic_box[0], pic_box[1]
    fx.shadow(
        canvas,
        card.getchannel("A"),
        (x, y),
        blur=s.S(14),
        opacity=0.25,
        offset=(s.S(6), s.S(10)),
    )
    canvas.alpha_composite(card, (x, y))
    tape = Image.new("RGBA", (round(s.S(200)), round(s.S(56))), (255, 214, 120, 190))
    stripes = ImageDraw.Draw(tape)
    for i in range(-tape.height, tape.width, max(4, round(s.S(22)))):
        stripes.line(
            (i, tape.height, i + tape.height, 0),
            fill=(255, 240, 200, 190),
            width=max(2, round(s.S(8))),
        )
    tape = tape.rotate(6, resample=Image.Resampling.BICUBIC, expand=True)
    canvas.alpha_composite(
        tape, (round(x + card.width / 2 - tape.width / 2), round(y - tape.height / 2))
    )
    pen = (36, 52, 104)
    s.write(canvas, (270, 170, 1190, 900), pen, 80)
    if s.name:
        font = s.name_font(max(1, round(44 * s.k * s.family.scale)))
        length = min(460, font.getlength(s.name) / s.kx)
        s.rect(canvas, (1300, 762, 1320 + length, 790), (255, 230, 90, 170), radius=8)
        s.line(canvas, s.name, (1310, 780), 44, pen, width=460)
    s.line(
        canvas,
        s.handle_text,
        (1310, 850 if s.name else 780),
        30,
        (90, 100, 140),
        kind="handle",
        width=460,
    )
    s.vector(
        canvas,
        pen,
        lambda d, P, S: fx.star(d, *P(1235, 700), S(26), 255, 5, 0.45),
        (1200, 665, 1270, 735),
    )
    return canvas


@layout("pixel", "ピクセル", "cute", ("dot",), number=26)
def pixel(s):
    canvas = fx.pixelate(fx.cover(s.image, s.size), 48, 16)
    canvas = Image.blend(canvas, fill(s.size, (16, 14, 40)), 0.62)
    stars = [(s.rng.uniform(0, 1920), s.rng.uniform(0, 480)) for _ in range(26)]
    for x, y in stars:
        size = s.rng.choice((6, 6, 10))
        s.rect(canvas, (x, y, x + size, y + size), (255, 255, 230, 200))
    window = (60, 560, 1860, 1020)
    s.rect(canvas, window, (10, 10, 30, 235))
    s.frame(canvas, window, (255, 255, 255), 8)
    s.frame(canvas, (78, 578, 1842, 1002), (255, 255, 255), 3)
    portrait = (110, 610, 470, 970)
    l, t, r, b = s.box(portrait)
    face = fx.pixelate(fx.fitted(s.image, (r - l, b - t), s.attachment), 36, 20)
    canvas.alpha_composite(face, (l + (r - l - face.width) // 2, t + (b - t - face.height) // 2))
    s.frame(canvas, portrait, (255, 255, 255), 4)
    if s.has_credit:
        label = s.name or s.handle_text
        font = s.name_font(max(1, round(40 * s.k * s.family.scale)))
        width = min(900, font.getlength(label) / s.kx + 70)
        tag = (110, 470, 110 + width, 548)
        s.rect(canvas, tag, (10, 10, 30, 255))
        s.frame(canvas, tag, (255, 255, 255), 6)
        s.line(canvas, label, (145, 525), 40, (255, 236, 140), width=width - 70)
    s.write(canvas, (520, 620, 1790, 940), (255, 255, 255), 66, valign="top")
    s.vector(
        canvas,
        (255, 255, 255),
        lambda d, P, S: d.polygon([P(1780, 960), P(1820, 960), P(1800, 985)], fill=255),
        (1770, 950, 1830, 995),
        factor=1,
    )
    return canvas


HOLO = (
    (255, 154, 214),
    (150, 220, 255),
    (255, 245, 160),
    (190, 160, 255),
    (140, 255, 214),
)


@layout("tcg", "トレカ", "cute", ("gothic", "mincho", "shippori", "maru"), number=27)
def tcg(s):
    canvas = fx.radial(s.size, (40, 28, 72), (10, 10, 20), center=(0.25, 0.5), radius=0.9)
    for _ in range(18):
        x, y = s.rng.uniform(900, 1880), s.rng.uniform(40, 1040)
        size = s.rng.uniform(6, 16)
        s.vector(
            canvas,
            (255, 255, 255, 160),
            lambda d, P, S, x=x, y=y, size=size: fx.star(d, *P(x, y), S(size), 255, 4, 0.3),
            (x - size - 2, y - size - 2, x + size + 2, y + size + 2),
        )
    card = (150, 70, 830, 1010)
    l, t, r, b = s.box(card)
    mask = fx.round_mask((r - l, b - t), s.S(34))
    fx.shadow(canvas, mask, (l, t), blur=s.S(36), color=(150, 90, 255), opacity=0.55)
    holo = fx.gradient((r - l, b - t), HOLO, 35)
    canvas.paste(holo, (l, t), mask)
    face = (172, 92, 808, 988)
    s.rect(canvas, face, (246, 241, 228), radius=24)
    s.rect(canvas, (192, 112, 788, 190), (40, 36, 52), radius=14)
    title = s.name or s.handle_text or "???"
    s.line(canvas, title, (215, 166), 38, (255, 255, 255), width=430)
    for i in range(3):
        x = 760 - i * 40
        s.vector(
            canvas,
            (255, 214, 102),
            lambda d, P, S, x=x: fx.star(d, *P(x, 151), S(17), 255, 5, 0.45),
            (x - 20, 131, x + 20, 171),
        )
    art = (192, 205, 788, 700)
    s.place(canvas, s.image, art, cover=True)
    al, at, ar, ab = s.box(art)
    shine = fx.gradient((ar - al, ab - at), [(255, 255, 255), (255, 255, 255)], 0)
    sheen = fx.gradient(
        (ar - al, ab - at),
        [(0, 0, 0), (255, 255, 255), (0, 0, 0)],
        30,
        steps=[0, 0.5, 1],
    )
    shine.putalpha(sheen.getchannel("R").point(lambda v: round(v * 0.28)))
    canvas.alpha_composite(shine, (al, at))
    s.frame(canvas, art, (40, 36, 52), 4)
    s.line(
        canvas,
        s.handle_text or "—",
        (210, 750),
        26,
        (90, 84, 100),
        kind="handle",
        width=560,
    )
    s.hline(canvas, 210, 770, 775, (200, 190, 175), 2)
    attack = zlib.crc32(s.text.encode()) % 3000 + 500
    s.line(canvas, f"ATK {attack}", (210, 950), 28, (40, 36, 52), kind="handle", width=280)
    s.line(
        canvas,
        f"DEF {len(s.text) * 10 % 3000 + 300}",
        (790, 950),
        28,
        (40, 36, 52),
        kind="handle",
        anchor="right",
        width=280,
    )
    s.line(canvas, s.date_text, (210, 830), 24, (120, 112, 128), kind="handle", width=560)
    s.write(canvas, (960, 170, 1800, 820), (255, 255, 255), 82)
    if s.name:
        s.line(canvas, "— " + s.name, (960, 920), 40, (220, 200, 255), width=820)
    return canvas
