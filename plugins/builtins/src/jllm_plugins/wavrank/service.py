"""1人分の wav 呼び出しランキング画像を作る（Pillow だけで描く）。

- 集計元はチャンネル書き出し（`data/exports/<channel>/messages.jsonl`）の生データ。
- 画像はブラウザを使わず Pillow で描く（x230 に Chrome が無くても動く）。
- 単体でも動く:
    python3 -m jllm_plugins.wavrank.service --user 123456789012345678 --archive /path/messages.jsonl
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


@dataclass(frozen=True)
class WavRankConfig:
    data_dir: Path = Path("data")
    channel_id: str = ""
    archive: Path | None = None
    sounds: Path | None = None
    out_dir: Path | None = None
    font: str = ""

    @property
    def archive_path(self) -> Path:
        if self.archive is not None:
            return self.archive
        if not self.channel_id:
            raise ValueError("channel_id または archive を指定してください")
        return self.data_dir / "exports" / self.channel_id / "messages.jsonl"

    @property
    def sounds_path(self) -> Path:
        return self.sounds or self.data_dir / "sounds.json"

    @property
    def output_dir(self) -> Path:
        return self.out_dir or self.data_dir / "wav_rank"

    @property
    def font_candidates(self) -> tuple[str, ...]:
        return (
            self.font,
            str(self.data_dir / "miq_fonts" / "NotoSansJP.ttf"),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        )


DEFAULT_CONFIG = WavRankConfig()

_KANA = {chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)}


def _fold_char(ch: str) -> str:
    out = []
    for folded in unicodedata.normalize("NFKC", ch):
        for item in folded.casefold():
            out.append(_KANA.get(item, item))
    return "".join(out)


def normalize(text: str) -> str:
    return "".join(_fold_char(c) for c in text)


def boundary_pattern(key: str) -> str:
    escaped = re.escape(key)
    if re.fullmatch(r"[a-z0-9_]+", key):
        return r"(?<![a-z0-9_])" + escaped + r"(?![a-z0-9_])"
    return escaped


_font_cache: dict[tuple[int, str, tuple[str, ...]], ImageFont.FreeTypeFont] = {}


def font(
    size: int,
    weight: str = "bold",
    config: WavRankConfig | None = None,
) -> ImageFont.FreeTypeFont:
    """使えるフォントを探して読む。可変フォントなら太さも寄せる。"""
    candidates = (config or DEFAULT_CONFIG).font_candidates
    key = (size, weight, candidates)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    loaded = None
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        for index in (0, 1, 2):
            try:
                loaded = ImageFont.truetype(candidate, size, index=index)
                break
            except OSError:
                continue
        if loaded is not None:
            break
    if loaded is None:
        loaded = ImageFont.load_default()
    if weight in ("bold", "black"):
        for name in ("Black", "ExtraBold", "Bold", "SemiBold"):
            try:
                loaded.set_variation_by_name(name)
                break
            except Exception:
                continue
    _font_cache[key] = loaded
    return loaded


def _vgradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        img.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img.resize((w, h))


def _glow(size: tuple, center: tuple, radius: int, color: tuple, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(
        [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
        fill=color + (alpha,),
    )
    return layer.filter(ImageFilter.GaussianBlur(radius // 2))


def _hgradient_bar(img: Image.Image, box, colors, radius: int) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    strip = Image.new("RGBA", (w, 1))
    for x in range(w):
        t = x / max(w - 1, 1)
        strip.putpixel(
            (x, 0),
            tuple(int(colors[0][i] + (colors[1][i] - colors[0][i]) * t) for i in range(3)) + (255,),
        )
    grad = strip.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(grad, (x0, y0), mask)


def compute(
    user_id: int | str,
    archive: str | None = None,
    sounds: str | None = None,
    top: int = 20,
    config: WavRankConfig | None = None,
) -> dict:
    """1人分の集計。archive は生データの messages.jsonl。"""
    config = config or DEFAULT_CONFIG
    archive_path = Path(archive) if archive else config.archive_path
    sounds_path = Path(sounds) if sounds else config.sounds_path
    if not archive_path.is_file():
        raise FileNotFoundError(f"書き出しが見つかりません: {archive_path}")
    if not sounds_path.is_file():
        raise FileNotFoundError(f"sounds.json が見つかりません: {sounds_path}")

    sounds_map = json.loads(sounds_path.read_text(encoding="utf-8"))
    keys = sorted(sounds_map, key=len, reverse=True)
    pats = {k: re.compile(boundary_pattern(normalize(k))) for k in keys}

    counts: collections.Counter = collections.Counter()
    names: collections.Counter = collections.Counter()
    msgs = fired = 0
    first = last = ""
    with archive_path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            author = rec.get("author") or {}
            if str(author.get("id")) != str(user_id):
                continue
            msgs += 1
            names[author.get("global_name") or author.get("username") or ""] += 1
            ts = rec.get("timestamp") or ""
            if ts:
                first = first or ts
                last = ts
            text = rec.get("content") or ""
            if not text:
                continue
            norm = normalize(text)
            hit = False
            for key in keys:
                n = len(pats[key].findall(norm))
                if n:
                    counts[key] += n
                    hit = True
            if hit:
                fired += 1

    return {
        "user": str(user_id),
        "name": names.most_common(1)[0][0] if names else str(user_id),
        "msgs": msgs,
        "fired": fired,
        "unique": len(counts),
        "rows": counts.most_common(top),
        "first": first[:10],
        "last": last[:10],
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def render(
    stats: dict,
    out_path: str | None = None,
    scale: int = 2,
    config: WavRankConfig | None = None,
) -> str:
    """ランキング画像を描いて保存し、パスを返す。"""
    config = config or DEFAULT_CONFIG

    def load_font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
        return font(size, weight, config)

    rows = stats["rows"]
    if not rows:
        raise ValueError("この人の wav 呼び出しが1件もありません")
    top3, rest = rows[:3], rows[3:]
    W = 1400
    row_h = 84
    H = 340 + 330 + len(rest) * row_h + 210

    def px(v: float) -> int:
        return int(round(v * scale))

    img = _vgradient(px(W), px(H), (10, 13, 22), (9, 12, 20)).convert("RGBA")
    img.alpha_composite(_glow((px(W), px(H)), (px(220), px(60)), px(700), (29, 58, 110), 210))
    img.alpha_composite(_glow((px(W), px(H)), (px(1200), px(120)), px(560), (74, 29, 99), 170))
    img.alpha_composite(_glow((px(W), px(H)), (px(700), px(H)), px(760), (18, 48, 73), 150))

    d = ImageDraw.Draw(img)
    f_eyebrow = load_font(px(21))
    f_title = load_font(px(62), "black")
    f_chip = load_font(px(23))
    f_medal = load_font(px(30), "black")
    f_pterm = load_font(px(50), "black")
    f_pnum = load_font(px(62), "black")
    f_ppct = load_font(px(50), "black")
    f_psub = load_font(px(21), "regular")
    f_head = load_font(px(22))
    f_rank = load_font(px(30))
    f_term = load_font(px(44))
    f_num = load_font(px(46), "black")
    f_pct = load_font(px(36), "black")
    f_occ = load_font(px(26), "regular")
    f_foot = load_font(px(22), "regular")

    M = px(56)
    d.text((M, px(46)), "DISCORD #雑談vc ・ wav 呼び出し", font=f_eyebrow, fill=(111, 168, 255))
    d.text((M, px(84)), f"{stats['name']} さんのランキング", font=f_title, fill=(240, 244, 255))

    chips = [
        f"投稿 {stats['msgs']:,}",
        f"wav 発火 {stats['fired']:,}",
        f"トリガー {stats['unique']} 語",
        f"{stats['first']} 〜 {stats['last']}",
    ]
    x, y = M, px(182)
    for text in chips:
        w = d.textlength(text, font=f_chip) + px(34)
        d.rounded_rectangle(
            [x, y, x + w, y + px(46)],
            radius=px(23),
            fill=(24, 32, 52),
            outline=(60, 92, 140),
            width=px(2),
        )
        d.text((x + px(17), y + px(10)), text, font=f_chip, fill=(169, 199, 255))
        x += w + px(12)

    pw = (px(W) - M * 2 - px(24) * 2) / 3
    palette = [
        ((255, 212, 99), (255, 233, 168)),
        ((216, 222, 233), (243, 246, 251)),
        ((232, 168, 124), (244, 201, 168)),
    ]
    for i, (key, n) in enumerate(top3):
        cx = M + i * (pw + px(24))
        cy = px(258)
        c1, c3 = palette[i]
        d.rounded_rectangle(
            [cx, cy, cx + pw, cy + px(300)],
            radius=px(26),
            fill=(27, 30, 39),
            outline=c1,
            width=px(3),
        )
        d.ellipse([cx + px(22), cy + px(22), cx + px(78), cy + px(78)], fill=c1)
        d.text((cx + px(50), cy + px(50)), str(i + 1), font=f_medal, fill=(16, 19, 28), anchor="mm")
        d.text((cx + px(22), cy + px(96)), key, font=f_pterm, fill=(238, 242, 252))
        d.text((cx + px(22), cy + px(158)), f"{n:,}", font=f_pnum, fill=(255, 255, 255))
        d.text(
            (cx + px(22) + d.textlength(f"{n:,}", font=f_pnum), cy + px(184)),
            "回",
            font=f_psub,
            fill=(157, 170, 196),
        )
        pct = 100.0 * n / max(stats["fired"], 1)
        d.text((cx + px(22), cy + px(226)), f"{pct:.1f}%", font=f_ppct, fill=c3)

    y = px(632)
    maxv = rows[0][1]
    bar_x = M + px(360)
    bar_w = px(W) - M - bar_x - px(210)
    d.text((M + px(30), y - px(40)), "順位", font=f_head, fill=(122, 134, 158), anchor="lm")
    d.text((M + px(78), y - px(40)), "語句", font=f_head, fill=(122, 134, 158), anchor="lm")
    d.text(
        (px(W) - M - px(120), y - px(40)), "回数", font=f_head, fill=(122, 134, 158), anchor="rm"
    )
    d.text((px(W) - M, y - px(40)), "発火比", font=f_head, fill=(122, 134, 158), anchor="rm")

    stripes = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(stripes)
    for i in range(0, len(rest), 2):
        ry = y + i * px(row_h)
        sd.rounded_rectangle(
            [M - px(14), ry - px(8), px(W) - M + px(14), ry + px(row_h) - px(18)],
            radius=px(14),
            fill=(255, 255, 255, 18),
        )
    img.alpha_composite(stripes)

    for i, (key, n) in enumerate(rest):
        ry = y + i * px(row_h)
        d.text(
            (M + px(30), ry + px(16)), str(i + 4), font=f_rank, fill=(120, 132, 156), anchor="lm"
        )
        d.text((M + px(78), ry + px(16)), key, font=f_term, fill=(234, 239, 250), anchor="lm")
        w = int(bar_w * math.sqrt(n / maxv))
        d.rounded_rectangle(
            [bar_x, ry + px(2), bar_x + bar_w, ry + px(34)], radius=px(17), fill=(38, 44, 60)
        )
        _hgradient_bar(
            img,
            (bar_x, ry + px(2), bar_x + max(w, px(6)), ry + px(34)),
            ((46, 107, 255), (138, 217, 255)),
            px(17),
        )
        d.text(
            (px(W) - M - px(112), ry + px(16)),
            f"{n:,}",
            font=f_num,
            fill=(255, 255, 255),
            anchor="rm",
        )
        d.text(
            (px(W) - M - px(96), ry + px(20)), "回", font=f_occ, fill=(150, 164, 190), anchor="lm"
        )
        d.text(
            (px(W) - M, ry + px(16)),
            f"{100.0 * n / max(stats['fired'], 1):.0f}%",
            font=f_pct,
            fill=(168, 203, 255),
            anchor="rm",
        )

    fy = px(H) - px(120)
    d.line([M, fy, px(W) - M, fy], fill=(38, 44, 60), width=px(2))
    d.text(
        (M, fy + px(18)),
        f"その時点の最新データ（{stats['first']} 〜 {stats['last']}）で集計",
        font=f_foot,
        fill=(135, 148, 174),
    )
    d.text(
        (M, fy + px(52)),
        "マッチングは bot 本体と同じ正規化＋単語境界。% は本人の wav 発火メッセージ比",
        font=f_foot,
        fill=(135, 148, 174),
    )
    d.text(
        (px(W) - M, fy + px(18)),
        f"生成 {stats['generated']}",
        font=f_foot,
        fill=(135, 148, 174),
        anchor="ra",
    )

    out = Path(out_path) if out_path else config.output_dir / f"{stats['user']}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, quality=95)
    return str(out)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="1人分の wav 呼び出しランキング画像")
    ap.add_argument("--user", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--archive", default="")
    ap.add_argument("--sounds", default="")
    ap.add_argument("--channel", default="")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--font", default="")
    args = ap.parse_args()
    config = WavRankConfig(
        data_dir=Path(args.data_dir),
        channel_id=args.channel,
        font=args.font,
    )
    stats = compute(args.user, args.archive or None, args.sounds or None, args.top, config)
    png = render(stats, args.out or None, args.scale, config)
    print(
        json.dumps(
            {
                **{k: v for k, v in stats.items() if k != "rows"},
                "png": png,
                "top3": [k for k, _ in stats["rows"][:3]],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
