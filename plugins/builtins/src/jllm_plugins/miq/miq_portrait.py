# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Portrait (9:16) and square layouts, numbered from 28.

Portrait coordinates are in a 1080x1920 design space, square ones in
1080x1080 (see ``miq_layouts.DESIGNS``).
"""

from __future__ import annotations

import random
import zlib

from PIL import Image

from . import miq_effects as fx
from .miq_designs import GOLD, HOLO, _graphemes
from .miq_layouts import fill, layout
from .miq_text import paint

# Rarity name, weight, frame colours, star count. UR/SSR shimmer.
RARITIES = (
    ("N", 40, ((176, 182, 192), (226, 230, 236)), 1),
    ("R", 30, ((70, 130, 240), (150, 200, 255)), 2),
    ("SR", 18, ((150, 90, 240), (226, 170, 255)), 3),
    ("SSR", 9, GOLD, 4),
    ("UR", 3, HOLO, 5),
)


def rarity(scene):
    """Gacha draw: the seed decides, so 🎲 can roll a rarer card."""

    seed = scene.seed if scene.seed is not None else zlib.crc32(scene.text.encode())
    rng = random.Random(f"{seed}:{scene.text}")
    value = rng.random() * sum(r[1] for r in RARITIES)
    for item in RARITIES:
        value -= item[1]
        if value < 0:
            return item
    return RARITIES[0]


def sparkles(s, canvas, area, count, color=(255, 255, 255, 170), size=(6, 16)):
    for _ in range(count):
        x, y = s.rng.uniform(area[0], area[2]), s.rng.uniform(area[1], area[3])
        r = s.rng.uniform(*size)
        s.vector(
            canvas,
            color,
            lambda d, P, S, x=x, y=y, r=r: fx.star(d, *P(x, y), S(r), 255, 4, 0.3),
            (x - r - 2, y - r - 2, x + r + 2, y + r + 2),
        )


@layout(
    "card",
    "ガチャカード",
    "cute",
    ("gothic", "mincho", "maru", "dela"),
    number=28,
    orientation="portrait",
)
def card(s):
    label, _, colors, stars = rarity(s)
    shiny = label in ("SSR", "UR")
    canvas = fx.radial(s.size, (44, 30, 80), (8, 8, 18), center=(0.5, 0.35), radius=0.9)
    sparkles(s, canvas, (0, 0, 1080, 1920), 30 if shiny else 12)
    body = (70, 90, 1010, 1830)
    l, t, r, b = s.box(body)
    mask = fx.round_mask((r - l, b - t), s.S(44))
    glow = colors[1] if len(colors) > 1 else colors[0]
    fx.shadow(canvas, mask, (l, t), blur=s.S(40), color=glow, opacity=0.6 if shiny else 0.35)
    canvas.paste(fx.gradient((r - l, b - t), list(colors), 35), (l, t), mask)
    face = (96, 116, 984, 1804)
    s.rect(canvas, face, (246, 241, 228), radius=30)
    ink = (40, 36, 52)
    # Title bar: name on the left, rarity on the right.
    s.rect(canvas, (120, 140, 960, 240), ink, radius=18)
    badge = s.name_font(max(1, round(54 * s.k * s.family.scale)))
    badge_w = badge.getlength(label) / s.k
    s.line(
        canvas,
        s.name or s.handle_text or "???",
        (150, 210),
        46,
        (255, 255, 255),
        width=760 - badge_w,
    )
    s.line(
        canvas,
        label,
        (935, 212),
        54,
        colors[1 if len(colors) > 1 else 0],
        anchor="right",
        width=200,
    )
    art = (120, 262, 960, 1002)
    s.place(canvas, s.image, art)
    if label != "N":
        al, at, ar, ab = s.box(art)
        sheen = fx.gradient(
            (ar - al, ab - at),
            [(0, 0, 0), (255, 255, 255), (0, 0, 0)],
            30,
            steps=[0, 0.5, 1],
        )
        shine = Image.new("RGBA", sheen.size, (255, 255, 255, 0))
        shine.putalpha(sheen.getchannel("R").point(lambda v: round(v * (0.34 if shiny else 0.2))))
        canvas.alpha_composite(shine, (al, at))
    s.frame(canvas, art, ink, 5)
    for i in range(stars):
        x = 540 + (i - (stars - 1) / 2) * 76
        s.vector(
            canvas,
            (255, 196, 60),
            lambda d, P, S, x=x: fx.star(d, *P(x, 1062), S(30), 255, 5, 0.45),
            (x - 34, 1028, x + 34, 1096),
        )
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (150, 1150),
            32,
            (110, 102, 120),
            kind="handle",
            width=780,
        )
    s.hline(canvas, 150, 930, 1176, (200, 190, 175), 2)
    s.write(canvas, (150, 1210, 930, 1660), ink, 74, align="center")
    s.hline(canvas, 150, 930, 1690, (200, 190, 175), 2)
    number = zlib.crc32((s.name + s.text).encode()) % 10000
    s.line(
        canvas,
        f"No.{number:04d}",
        (150, 1752),
        30,
        (120, 112, 128),
        kind="handle",
        width=360,
    )
    s.line(
        canvas,
        s.date_text,
        (930, 1752),
        30,
        (120, 112, 128),
        kind="handle",
        anchor="right",
        width=360,
    )
    return canvas


@layout(
    "story",
    "ストーリー",
    "cool",
    ("gothic", "maru", "mincho", "dela"),
    number=29,
    orientation="portrait",
)
def story(s):
    canvas = fx.backdrop(s.image, s.size, s.S(60), darken=0.45, saturation=1.2)
    shade = Image.new("RGBA", s.size, (0, 0, 0, 0))
    shade.putalpha(
        fx.gradient(
            s.size, [(40, 40, 40), (0, 0, 0), (120, 120, 120)], 90, steps=[0, 0.45, 1]
        ).getchannel("R")
    )
    canvas.alpha_composite(shade)
    cx, cy, r = 540, 400, 150
    ring = (cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10)
    rl, rt, rr, rb = s.box(ring)
    side = min(rr - rl, rb - rt)
    disc = fx.ellipse_mask((side, side))
    ox, oy = round(s.X(cx) - side / 2), round(s.Y(cy) - side / 2)
    canvas.alpha_composite(fx.colored(disc, (255, 255, 255)), (ox, oy))
    s.place(canvas, s.image, (cx - r, cy - r, cx + r, cy + r), circle=True, cover=True)
    white = (255, 255, 255)
    y = 640
    if s.name:
        s.line(canvas, s.name, (cx, y), 50, white, anchor="center", width=900)
        y += 62
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (cx, y),
            32,
            (220, 220, 225),
            kind="handle",
            anchor="center",
            width=900,
        )
    region = s.box((110, 820, 970, 1640))
    result = s.fit(region, 92)
    mask = s.text_mask(result, region, align="center")
    canvas.alpha_composite(fx.glow(mask, (0, 0, 0), s.S(14), 1.6))
    paint(canvas, result, region, white, align="center")
    s.line(
        canvas,
        s.date_text,
        (cx, 1800),
        28,
        (220, 220, 225),
        kind="handle",
        anchor="center",
        width=600,
    )
    return canvas


@layout(
    "poster",
    "ポスター",
    "cool",
    ("dela", "gothic", "mincho", "shippori"),
    number=30,
    orientation="portrait",
)
def poster(s):
    base = fx.shade(fx.accent(s.image), value=0.32, saturation=0.9)
    canvas = fill(s.size, base)
    photo = (0, 0, 1080, 1100)
    l, t, r, b = s.box(photo)
    picture = fx.cover(s.image, (r - l, b - t), (0.5, 0.4)) if not s.attachment else None
    if picture is None:
        canvas.alpha_composite(fx.backdrop(s.image, (r - l, b - t), s.S(50), darken=0.3), (l, t))
        s.place(canvas, s.image, (60, 60, 1020, 1040))
    else:
        canvas.alpha_composite(picture, (l, t))
    fade = Image.new("RGBA", (r - l, b - t), fx.rgb(base) + (0,))
    fade.putalpha(
        fx.gradient(
            (r - l, b - t),
            [(0, 0, 0), (0, 0, 0), (255, 255, 255)],
            90,
            steps=[0, 0.6, 1],
        ).getchannel("R")
    )
    canvas.alpha_composite(fade, (l, t))
    ink = fx.ink_for(base)
    s.write(canvas, (90, 1110, 990, 1690), ink, 110)
    s.hline(canvas, 90, 330, 1745, ink, 6)
    if s.name:
        s.line(canvas, s.name, (90, 1820), 46, ink, width=900)
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (90, 1872 if s.name else 1820),
            30,
            fx.mix(ink, base, 0.4),
            kind="handle",
            width=900,
        )
    return canvas


@layout(
    "tategaki_tall",
    "縦長縦書き",
    "cool",
    ("shippori", "mincho", "klee"),
    number=31,
    orientation="portrait",
)
def tategaki_tall(s):
    from .miq_vertical import draw_vertical, suits_vertical

    paper = (244, 238, 226)
    canvas = fill(s.size, paper)
    fibres = fx.soft_blur(fx.noise(s.size, 70), s.S(1.2))
    canvas.alpha_composite(fx.colored(fibres.point(lambda v: max(0, v - 150)), (160, 130, 90)))
    ink = (28, 24, 22)
    vermilion = (192, 52, 36)
    s.frame(canvas, (60, 60, 1020, 1860), (120, 100, 80), 2)
    if s.aa or not suits_vertical(s.text):
        s.write(canvas, (120, 140, 960, 1440), ink, 96)
    else:
        draw_vertical(
            canvas,
            s,
            s.box((120, 130, 960, 1480)),
            ink,
            max_px=180 * s.k * s.family.scale,
        )
    s.hline(canvas, 120, 960, 1540, (120, 100, 80, 120), 1)
    s.place(canvas, s.image, (120, 1590, 360, 1830))
    s.frame(canvas, (120, 1590, 360, 1830), (60, 50, 40), 2)
    if s.name:
        s.line(canvas, s.name, (400, 1690), 46, ink, width=420)
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (400, 1690 + (60 if s.name else 0)),
            28,
            (120, 110, 100),
            kind="handle",
            width=420,
        )
    if s.name:
        s.rect(canvas, (840, 1660, 960, 1780), vermilion, radius=14)
        s.frame(canvas, (848, 1668, 952, 1772), paper, 3, radius=10)
        first = next(iter(_graphemes(s.name)), "")
        s.line(canvas, first, (900, 1748), 66, paper, anchor="center", width=100)
    return canvas


@layout(
    "polaroid",
    "ポラロイド",
    "cute",
    ("klee", "maru", "gothic"),
    number=32,
    orientation="square",
)
def polaroid(s):
    canvas = fx.gradient(s.size, [(250, 228, 214), (232, 222, 250)], 30)
    sparkles(s, canvas, (0, 0, 1080, 1080), 10, (255, 255, 255, 200), (8, 18))
    sheet = Image.new("RGBA", s.size, (0, 0, 0, 0))
    frame = (170, 70, 910, 1010)
    l, t, r, b = s.box(frame)
    mask = fx.round_mask((r - l, b - t), s.S(10))
    sheet.paste(Image.new("RGBA", mask.size, (253, 252, 248, 255)), (l, t), mask)
    s.place(sheet, s.image, (210, 110, 870, 590), cover=True)
    ink = (60, 52, 70)
    s.write(sheet, (220, 620, 860, 900), ink, 64, align="center")
    if s.name:
        s.line(
            sheet,
            "— " + s.name,
            (860, 965),
            34,
            (130, 120, 140),
            anchor="right",
            width=620,
        )
    angle = s.rng.uniform(-4, 4)
    # Rotate only the card (not the whole canvas): BICUBIC rotation is costly.
    card = sheet.crop((l, t, r, b)).rotate(angle, Image.Resampling.BILINEAR, expand=True)
    xy = (round((l + r - card.width) / 2), round((t + b - card.height) / 2))
    fx.shadow(canvas, card.getchannel("A"), xy, blur=s.S(18), opacity=0.3, offset=(0, s.S(10)))
    canvas.alpha_composite(card, (max(0, xy[0]), max(0, xy[1])), (max(0, -xy[0]), max(0, -xy[1])))
    # A strip of tape across the top edge.
    tl, tt, tr, tb = s.box((440, 40, 640, 110))
    tape = Image.new("RGBA", (tr - tl, tb - tt), (255, 236, 160, 170))
    tape = tape.rotate(angle - 6, Image.Resampling.BICUBIC, expand=True)
    canvas.alpha_composite(
        tape,
        (round((tl + tr - tape.width) / 2), max(0, round((tt + tb - tape.height) / 2))),
    )
    return canvas


@layout(
    "post",
    "SNS投稿",
    "classic",
    ("gothic", "maru", "mincho"),
    number=33,
    orientation="square",
)
def post(s):
    canvas = fill(s.size, (255, 255, 255))
    ink, sub = (15, 20, 25), (83, 100, 113)
    s.place(canvas, s.image, (80, 80, 220, 220), circle=True, cover=True)
    if s.name:
        s.line(canvas, s.name, (250, 140), 44, ink, width=760)
    if s.handle:
        s.line(
            canvas,
            s.handle_text,
            (250, 196 if s.name else 140),
            32,
            sub,
            kind="handle",
            width=760,
        )
    s.write(canvas, (80, 270, 1000, 860), ink, 78, valign="top")
    s.line(canvas, s.date_text, (80, 925), 30, sub, kind="handle", width=500)
    s.hline(canvas, 80, 1000, 960, (220, 226, 230), 2)
    likes = zlib.crc32(s.text.encode()) % 9000 + 100
    reposts = likes // 7 + 3
    s.line(
        canvas,
        f"{reposts:,} 件の再投稿   {likes:,} 件のいいね",
        (80, 1020),
        30,
        sub,
        kind="handle",
        width=920,
    )
    s.vector(
        canvas,
        (249, 24, 128),
        lambda d, P, S: fx.heart(d, *P(985, 1006), S(44), 255),
        (950, 970, 1020, 1040),
    )
    return canvas
