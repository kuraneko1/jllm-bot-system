# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""Turn raw Discord message content into the text a quote image should show.

Markdown markers are removed (the emphasis itself cannot be kept: an image
uses one typeface), mentions become names, timestamps become dates and
spoilers become blacked-out bars. Custom emoji tags are kept for rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime

import regex

from .miq_words import GRAPHEME

BLOCK = "■"
MENTION = regex.compile(r"<@!?(\d+)>|<@&(\d+)>|<#(\d+)>")
TIMESTAMP = regex.compile(r"<t:(-?\d{1,13})(?::([tTdDfFR]))?>")
SPOILER = regex.compile(r"\|\|(.+?)\|\|", regex.S)
CODE_BLOCK = regex.compile(r"```(?:[\w+-]*\n)?(.*?)```", regex.S)
INLINE_CODE = regex.compile(r"`([^`\n]+)`")
LINK = regex.compile(r"\[([^\]\n]+)\]\(<?(https?://[^)\s>]+)>?\)")
ANGLE_URL = regex.compile(r"<(https?://[^\s>]+)>")
HEADING = regex.compile(r"^(?:#{1,3}|-#)\s+", regex.M)
QUOTE = regex.compile(r"^>>>\s?|^>\s", regex.M)
LIST = regex.compile(r"^(\s*)[-*]\s+", regex.M)
# Emphasis pairs, longest first; they never cross a line break.
EMPHASIS = [
    regex.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"),
    regex.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    regex.compile(r"__(?=\S)(.+?)(?<=\S)__"),
    regex.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
    regex.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"),
    regex.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"),
]
ESCAPE = regex.compile(r"\\([*_~`|>#\-\\\[\]()])")
STYLES = {
    "t": "%H:%M",
    "T": "%H:%M:%S",
    "d": "%Y/%m/%d",
    "D": "%Y年%m月%d日",
    "f": "%Y年%m月%d日 %H:%M",
    "F": "%Y年%m月%d日 %H:%M",
}


def _relative(moment, now):
    seconds = (now - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    for size, unit in (
        (86400 * 365, "年"),
        (86400 * 30, "か月"),
        (86400, "日"),
        (3600, "時間"),
        (60, "分"),
    ):
        if seconds >= size:
            amount = int(seconds // size)
            return f"{amount}{unit}{'後' if future else '前'}"
    return "たった今"


def clean(text, mentions=None, *, now=None, tz=None):
    """Discord content → plain quote text.

    ``mentions`` maps user/role/channel IDs (str or int) to display names.
    Unknown mentions fall back to ``@ユーザー`` / ``@ロール`` / ``#チャンネル``.
    """

    names = {str(k): str(v) for k, v in (mentions or {}).items()}
    now = now or datetime.now(UTC)
    tz = tz or datetime.now().astimezone().tzinfo

    protected = []

    def keep(value):
        protected.append(value)
        return f"\uf8f0{len(protected) - 1}\uf8f1"

    # The placeholder alphabet is private-use, but users can still type it.
    # Protect the sentinels themselves first so they are restored, not read.
    text = regex.sub(r"[\uf8f0\uf8f1]", lambda m: keep(m[0]), text)

    # Code keeps its characters verbatim (only the fences go).
    text = CODE_BLOCK.sub(lambda m: keep(m[1].strip("\n")), text)
    text = INLINE_CODE.sub(lambda m: keep(m[1]), text)
    text = ESCAPE.sub(lambda m: keep(m[1]), text)

    def mention(m):
        if m[1]:
            return "@" + names.get(m[1], "ユーザー")
        if m[2]:
            return "@" + names.get(m[2], "ロール")
        return "#" + names.get(m[3], "チャンネル")

    text = MENTION.sub(lambda m: keep(mention(m)), text)

    def stamp(m):
        try:
            moment = datetime.fromtimestamp(int(m[1]), UTC)
        except OverflowError, OSError, ValueError:
            return m[0]
        if (m[2] or "f") == "R":
            return _relative(moment, now)
        return moment.astimezone(tz).strftime(STYLES[m[2] or "f"])

    text = TIMESTAMP.sub(lambda m: keep(stamp(m)), text)
    text = SPOILER.sub(
        lambda m: keep(BLOCK * max(1, len(GRAPHEME.findall(m[1].strip()))) if m[1].strip() else ""),
        text,
    )
    text = LINK.sub(r"\1", text)
    text = ANGLE_URL.sub(r"\1", text)
    text = HEADING.sub("", text)
    text = QUOTE.sub("", text)
    text = LIST.sub(r"\1・", text)
    for pattern in EMPHASIS:
        previous = None
        while previous != text:
            previous = text
            text = pattern.sub(r"\1", text)
    text = regex.sub(
        r"\uf8f0(\d+)\uf8f1",
        lambda m: protected[int(m[1])] if int(m[1]) < len(protected) else m[0],
        text,
    )
    # Discord collapses nothing, but trailing blank lines only waste space.
    return text.strip("\n")
