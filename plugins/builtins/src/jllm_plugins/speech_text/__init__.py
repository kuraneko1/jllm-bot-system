"""読み上げ本文のインライントークン解析。

``f2``（速度）と ``ec3`` など（音声効果）の適用範囲を決める。
bot 本体（cogs/reader.py）とダッシュボードの実験場が同じ規約を使えるよう、
discord に依存しないこのモジュールへ切り出している。

適用範囲の規約:
    text1 text2 ec3   → 直前の区間＝ここまでの本文すべてに掛かる
    text1 ec3 text2   → text1 だけ
    text1 (text2) ec3 → 括弧内だけ
    ((text)ec3)bs20   → 入れ子。内側から順に適用

秒数 3s（小数点可）は直前の区間の再生上限になる（A 3s,B 6s と続けられる）。
ピッチだけは符号付きで、pi-3 と -pi3 のどちらも -3 半音になる。
"""

import math
import re

COMMAND_TOKEN_PATTERN = re.compile(
    r"[ \u3000]([fF][0-9]*\.?[0-9]+|[fF]|[0-9]+(?:\.[0-9]+)?|[sSｓＳ])$"
)

_EFFECT_CODES = "rb|ecl|ec|ch|vi|lo|bs|pi|rv|lm|wd|ph|ex|kk|tc"

# 効果トークン。pi は符号付きで、pi-3（数字に符号）と -pi3（コードの前に
# 符号）のどちらでも下降になる。それ以外の効果に付いた符号は無視する。
_TOKEN_BODY = (
    r"(?:[fF](?P<number>[0-9]*\.?[0-9]+)?|"
    r"(?P<seconds>[0-9]+(?:\.[0-9]+)?)[sS]|"
    r"(?P<neg>-)?(?P<code>" + _EFFECT_CODES + r")(?P<digits>-?\d{1,3})?)"
)

# 秒数は「3s, テキスト」のようにカンマ区切りでも書ける。
_LOOKAHEAD = r"(?=[ \u3000]|$|\(|,|、|，)"

PIECE_TOKEN_PATTERN = re.compile(
    r"(?:^|[ \u3000])" + _TOKEN_BODY + _LOOKAHEAD,
    re.IGNORECASE,
)

# 閉じ括弧の直後に続くトークン。グループの中身は別途スキャンして求める。
GROUP_TOKEN_PATTERN = re.compile(
    r"[ \u3000]*" + _TOKEN_BODY + _LOOKAHEAD,
    re.IGNORECASE,
)

_OPEN_PARENS = "(（"
_CLOSE_PARENS = ")）"


def _find_groups(text: str) -> list[tuple[int, int, str, re.Match]]:
    """括弧グループを (開始, 終了, 中身, 直後のトークン) で返す。入れ子対応。

    閉じ括弧の直後にトークン（fN / rbN 等）が続くものだけをグループとして
    扱う。入れ子の内側は外側グループの中身として再帰的に解析するため、
    ここでは最も外側のものだけを返す。
    """
    groups: list[tuple[int, int, str, re.Match]] = []
    index = 0
    length = len(text)

    while index < length:
        if text[index] not in _OPEN_PARENS:
            index += 1
            continue

        depth = 1
        cursor = index + 1
        while cursor < length and depth:
            if text[cursor] in _OPEN_PARENS:
                depth += 1
            elif text[cursor] in _CLOSE_PARENS:
                depth -= 1
            cursor += 1

        if depth:
            # 閉じられていない括弧はグループにしない
            index += 1
            continue

        token = GROUP_TOKEN_PATTERN.match(text, cursor)
        if token:
            groups.append((index, token.end(), text[index + 1 : cursor - 1], token))
        index = cursor

    return groups


def _parse_speed_pieces(
    text: str,
    default: float,
) -> list[tuple[str, float]]:
    """文中速度トークンで区切る。トークンは直前の区間に適用する。

    例: "A f2 B f4" → [("A", 2.0), ("B", 4.0)]。
    トークンのない区間は default。空区間は落とす。
    """
    return [(chunk, speed) for chunk, speed, _ in _parse_pieces(text, default)]


def _effect(code: str, digits, negative) -> tuple[str, float]:
    """トークンの効果を (コード, 強さ) にする。

    pi だけ負の強さを許す（半音の下降）。-pi3 と pi-3 は同じ -3。
    それ以外の効果に付いた符号は無視し、強さは 1〜20 に丸める。
    """
    count = int(digits) if digits else 1

    if negative:
        count = -abs(count)

    code = code.lower()

    if code == "pi":
        # 2 オクターブまで。0 は「符号だけ」なので 1 半音にする。
        return code, float(min(24, max(-24, count))) if count else 1.0

    return code, float(min(20, max(1, abs(count))))


def _seconds_value(value) -> float | None:
    """秒数トークンの値を 0.1〜3600 秒に丸める（不正なら None）。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(seconds) or seconds <= 0:
        return None

    return min(3600.0, seconds)


def _parse_pieces_ex(
    text: str,
    default: float,
) -> list[tuple[str, float, tuple, float | None]]:
    """文中トークン（速度 fN・効果 rbN・秒数 3.5s）で区切る。

    戻り値は (本文, 速度, 効果, 秒数)。秒数はその区間の再生上限で、
    None は無制限。3s は直前の区間に掛かり、カンマで区切って続けられる
    （A 3s,B 6s）。速度トークンは直前の区間の速度を決め、効果トークンは
    直前の区間に付く。効果トークンが続くときは同じ区間にまとまる。
    空区間の効果トークンは次の区間へ持ち越す。
    """
    pieces: list[tuple[str, float, tuple, float | None]] = []
    position = 0
    pending: list[tuple[str, float]] = []
    pending_seconds: float | None = None
    chain = False
    trim = " \u3000,、，"

    group_matches = _find_groups(text)

    def in_group(start: int) -> bool:
        return any(group[0] <= start < group[1] for group in group_matches)

    events: list[tuple[int, str, object]] = sorted(
        [
            (match.start(), "token", match)
            for match in PIECE_TOKEN_PATTERN.finditer(text)
            if not in_group(match.start())
        ]
        + [(group[0], "group", group) for group in group_matches]
    )

    for _, kind, item in events:
        if kind == "group":
            start, end, inner, token = item
            pre = text[position:start].strip(trim)

            if pre:
                pieces.append((pre, default, tuple(pending), pending_seconds))
                pending = []
                pending_seconds = None
                chain = False

            group_speed = None
            group_effect = None
            group_seconds = None

            if token.group("seconds") is not None:
                group_seconds = _seconds_value(token.group("seconds"))
            elif token.group("code") is not None:
                group_effect = _effect(
                    token.group("code"),
                    token.group("digits"),
                    token.group("neg"),
                )
            else:
                try:
                    group_speed = (
                        float(token.group("number")) if token.group("number") else 1.5
                    )
                except ValueError:
                    group_speed = default

                group_speed = max(0.1, min(100.0, group_speed))

            base = len(pieces)

            for chunk, speed, effects, seconds in _parse_pieces_ex(inner, default):
                if group_seconds is not None:
                    seconds = group_seconds

                if group_speed is not None:
                    pieces.append((chunk, group_speed, effects, seconds))
                elif group_effect is not None:
                    pieces.append(
                        (chunk, speed, tuple(effects) + (group_effect,), seconds)
                    )
                else:
                    pieces.append((chunk, speed, effects, seconds))

            if pending and len(pieces) > base:
                head, speed, effects, seconds = pieces[base]
                pieces[base] = (head, speed, tuple(pending) + tuple(effects), seconds)
                pending = []

            if pending_seconds is not None and len(pieces) > base:
                head, speed, effects, _seconds = pieces[base]
                pieces[base] = (head, speed, effects, pending_seconds)
                pending_seconds = None

            position = end
            chain = False
            continue

        match = item
        chunk = text[position : match.start()].strip(trim)
        number = match.group("number")
        seconds_token = match.group("seconds")
        code = match.group("code")
        digits = match.group("digits")

        if seconds_token is not None:
            seconds = _seconds_value(seconds_token)

            if chunk:
                pieces.append((chunk, default, tuple(pending), seconds))
                pending = []
                pending_seconds = None
                chain = True
            elif chain and pieces:
                head, speed, effects, _current = pieces[-1]
                pieces[-1] = (head, speed, effects, seconds)
            else:
                pending_seconds = seconds
                chain = True

        elif code is not None:
            effect = _effect(code, digits, match.group("neg"))

            if chunk:
                pieces.append(
                    (chunk, default, tuple(pending + [effect]), pending_seconds)
                )
                pending = []
                pending_seconds = None
                chain = True
            elif chain and pieces:
                head, speed, effects, seconds = pieces[-1]
                pieces[-1] = (head, speed, tuple(effects) + (effect,), seconds)
            else:
                pending.append(effect)
                chain = True
        else:
            try:
                speed = float(number) if number else 1.5
            except ValueError:
                speed = default

            if speed <= 0:
                speed = 1.5

            if chunk:
                pieces.append(
                    (
                        chunk,
                        max(0.1, min(100.0, speed)),
                        tuple(pending),
                        pending_seconds,
                    )
                )

            pending = []
            pending_seconds = None
            chain = False

        position = match.end()

    tail = text[position:].strip(trim)

    if tail:
        pieces.append((tail, default, tuple(pending), pending_seconds))
    elif pending and pieces:
        head, speed, effects, seconds = pieces[-1]
        pieces[-1] = (head, speed, tuple(effects) + tuple(pending), seconds)

    return pieces


def _parse_pieces(
    text: str,
    default: float,
) -> list[tuple[str, float, tuple]]:
    """秒数を落とした従来の 3-tuple 版（既存の呼び出し向け）。"""
    return [
        (chunk, speed, effects)
        for chunk, speed, effects, _seconds in _parse_pieces_ex(text, default)
    ]


def _apply_trailing_speed(pieces, speed):
    """文末の速度指定を、最後のピース（＝直前の区間全体）に掛ける。

    文末の f2 は parse_commands が剥がして渡ってくる。区間の途中に
    トークンが無ければ、その区間＝ここまでの本文すべてになる。
    """
    if not pieces or speed is None:
        return pieces

    chunk, _current, effects, *rest = pieces[-1]
    pieces[-1] = (chunk, speed, effects, *rest)

    return pieces


def parse_commands(
    content: str,
) -> tuple[str, float | None, bool] | None:
    text = content.rstrip(" \u3000")

    stop = False
    speed = None
    speed_explicit = False

    while True:
        match = COMMAND_TOKEN_PATTERN.search(text)

        if match is None:
            break

        token = match.group(1)
        text = text[: match.start()].rstrip(" \u3000")

        if token in "sSｓＳ":
            stop = True
        else:
            speed_explicit = token[0] in "fF"
            number = re.sub(r"^[fF]", "", token)
            speed = float(number) if number else 1.5

    if not text:
        return None

    if not stop and not speed_explicit:
        return None

    if speed is not None:
        if speed <= 0:
            speed = 1.5
        else:
            speed = max(0.1, min(100.0, speed))

    return text, speed, stop
