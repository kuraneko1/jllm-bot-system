"""miq のコマンド解釈（miq / miqmiq3 縦 / miq vs / miq台帳）。

Discord を触らない純粋な関数だけを置く（テストしやすい）。

- 繰り返しは件数: miqmiq = 返信先の人と、その直前の発言まで（最大10件）
- 数字は構図番号: miq12 / miq 12
- 比率・系統・掛け合いの語を後ろに足せる: miq 縦 / miq cute / miq vs
- miq台帳（台帳）/ miq help（使い方）は返信不要

知らない語が付いた投稿はコマンドとして扱わない（miq ってなに？ は普通の会話のまま）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

MIQ_WORDS = ("miq",)

#: 繰り返しで指定できる上限（miq を10回）
MAX_COUNT = 10
#: Discord のインラインコード用（ソースにそのまま書かない）
B = chr(96)

OPTIONS = {
    "orientation": {
        "portrait": ("縦", "たて", "tall", "portrait", "9:16", "縦長"),
        "landscape": ("横", "よこ", "wide", "landscape", "16:9", "横長"),
        "square": ("正方形", "四角", "square", "1:1"),
    },
    "mood": {
        "vs": ("vs", "対戦", "バトル", "battle", "ビリビリ"),
        "love": ("love", "恋", "ラブ", "らぶ", "♡", "♥", "❤", "❤️", "💕", "ハート"),
        "talk": ("talk", "会話", "chat", "トーク"),
    },
    "category": {
        "cool": ("cool", "かっこいい", "カッコいい", "カッコイイ"),
        "cute": ("cute", "かわいい", "可愛い", "カワイイ"),
        "classic": ("classic", "定番"),
    },
}
LOOKUP = {
    word: (field, value)
    for field, values in OPTIONS.items()
    for value, words in values.items()
    for word in words
}


@dataclass(frozen=True)
class Command:
    """1回の呼び出し。kind は quote / catalog / help。"""

    kind: str = "quote"
    count: int = 1
    layout: int | None = None
    orientation: str | None = None
    category: str | None = None
    mood: str | None = None


def normalize(content: str) -> str:
    """全角英数と連続する空白をそろえる（Ｍｉｑ　縦 も受ける）。"""

    text = unicodedata.normalize("NFKC", content or "").strip().casefold()

    return re.sub(r"\s+", " ", text)


@lru_cache(maxsize=8)
def _forms(words: tuple[str, ...]):
    """単語ごとの正規表現・台帳・使い方の綴り（設定が変わらなければ使い回す）。"""

    word = words[0]
    quoted = tuple(re.escape(item) for item in sorted(words, key=len, reverse=True))
    repeat = re.compile(
        rf"(?P<rep>(?:{'|'.join(quoted)})+)(?P<num>\d+)?(?:\s+(?P<opts>.+))?",
        re.S,
    )
    catalog = {
        f"{word}台帳",
        f"{word}一覧",
        f"{word}list",
        f"{word} list",
        f"{word} 台帳",
        f"{word} 一覧",
        f"{word} カタログ",
    }
    help_words = {
        f"{word} help",
        f"{word} ヘルプ",
        f"{word}help",
        f"{word} 使い方",
        f"{word}使い方",
    }

    return repeat, catalog, help_words, word


def parse(content: str, words=MIQ_WORDS) -> Command | None:
    """コマンドとして解釈できなければ None（＝ただの会話）。"""

    words = tuple(normalized for word in words if (normalized := normalize(word)))
    if not words:
        return None
    repeat, catalog, help_words, _ = _forms(words)
    text = normalize(content)

    if not text:
        return None
    if text in catalog:
        return Command("catalog")
    if text in help_words:
        return Command("help")

    matched = repeat.fullmatch(text)

    if matched is None:
        return None

    layout = int(matched["num"]) if matched["num"] else None
    found: dict[str, str] = {}

    for option in (matched["opts"] or "").split():
        if option.isdigit() and layout is None:
            layout = int(option)
            continue

        hit = LOOKUP.get(option)

        if hit is None:
            # 「miq ってなに？」のような普通の文を横取りしない
            return None

        found[hit[0]] = hit[1]

    word_pattern = "|".join(re.escape(item) for item in sorted(words, key=len, reverse=True))
    count = min(MAX_COUNT, len(re.findall(word_pattern, matched["rep"])))

    return Command(
        "quote",
        count=count,
        layout=layout,
        orientation=found.get("orientation"),
        category=found.get("category"),
        mood=found.get("mood"),
    )


def usage(words=MIQ_WORDS) -> str:
    """Discord にそのまま出せる使い方。"""

    word = words[0]
    code = B + word + B
    twice = B + word + word + B
    thrice = B + word + word + word + B

    return (
        f"**{word} の使い方**（引用したいメッセージに返信して送信）\n"
        f"{code} 1件 ／ {twice} {thrice}… その人の直前の発言もまとめて1つに"
        "（最大10件・間の他人の発言は飛ばす）\n"
        f"{B}{word}12{B} 構図を番号で指定（番号は {B}{word}台帳{B} で一覧）\n"
        f"{B}{word} 縦{B} {B}{word} 正方形{B} {B}{word} 横{B} 比率 ／ "
        f"{B}{word} cool{B} 系統\n"
        f"{B}{word} vs{B} {B}{word} love{B} {B}{word} talk{B} 返信相手との掛け合い\n"
        f"組み合わせ例: {B}{word}{word} 縦 cute{B}、{B}{word} vs 縦{B}、{B}{word}28{B}"
    )
