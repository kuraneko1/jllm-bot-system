"""読み上げ本文のインライントークン解析。

``f2``（速度）と ``ec3`` など（音声効果）の適用範囲を決める。
bot 本体（cogs/reader.py）とダッシュボードの実験場が同じ規約を使えるよう、
discord に依存しないこのモジュールへ切り出している。

適用範囲の規約:
    text1 text2 ec3   → 直前の区間＝ここまでの本文すべてに掛かる
    text1 ec3 text2   → text1 だけ
    text1 (text2) ec3 → 括弧内だけ
    ((text)ec3)bs20   → 入れ子。内側から順に適用
"""

import re

COMMAND_TOKEN_PATTERN = re.compile(
    r"[ \u3000]([fF][0-9]*\.?[0-9]+|[fF]|[0-9]+(?:\.[0-9]+)?|[sSｓＳ])$"
)

PIECE_TOKEN_PATTERN = re.compile(
    r"(?:^|[ \u3000])(?:[fF]([0-9]*\.?[0-9]+)?|"
    r"(rb|ecl|ec|ch|vi|lo|bs|pi|rv|lm|wd|ph|ex|kk|tc)(\d{1,3})?)(?=[ \u3000]|$|\()",
    re.IGNORECASE,
)

# 閉じ括弧の直後に続くトークン。グループの中身は別途スキャンして求める。
GROUP_TOKEN_PATTERN = re.compile(
    r"[ \u3000]*(?:[fF]([0-9]*\.?[0-9]+)?|"
    r"(rb|ecl|ec|ch|vi|lo|bs|pi|rv|lm|wd|ph|ex|kk|tc)(\d{1,3})?)(?=[ \u3000]|$|\()",
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


def _parse_pieces(
    text: str,
    default: float,
) -> list[tuple[str, float, tuple]]:
    """文中トークン（速度 fN・効果 rbN 等）で区切る。

    例: "A f2 B f4" → [("A", 2.0, ()), ("B", 4.0, ())]。
    例: "A rb B rb10" → [("A", default, (("rb", 1.0),)), ("B", default, (("rb", 10.0),))]。
    速度トークンは直前の区間の速度を決め、効果トークンは直前の区間に付く。
    効果トークンが続くときは同じ区間にまとまる。空区間の速度トークンは
    落ち、空区間の効果トークンは次の区間へ持ち越す。
    末尾に残った効果は最後の区間に付く。
    `(テキスト)fN` と書くと括弧内全体に速度Nがかかり（中身の効果は維持）、
    `(テキスト)rbN` のように効果コマンドでも全体に付く。
    括弧は入れ子にでき、内側から順に適用される
    （`((本文)ec3)bs20` は `(本文)ec3 bs20` と同じ）。
    """
    pieces: list[tuple[str, float, tuple]] = []
    position = 0
    pending: list[tuple[str, float]] = []
    chain = False

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
            pre = text[position:start].strip(" \u3000")

            if pre:
                pieces.append((pre, default, tuple(pending)))
                pending = []
                chain = False

            try:
                group_speed = float(token.group(1)) if token.group(1) else 1.5
            except ValueError:
                group_speed = default

            group_speed = max(0.1, min(100.0, group_speed))
            group_effect = None

            if token.group(2) is not None:
                count = int(token.group(3)) if token.group(3) else 1
                group_effect = (
                    token.group(2).lower(),
                    float(min(max(count, 1), 20)),
                )
                group_speed = None

            base = len(pieces)

            for chunk, speed, effects in _parse_pieces(inner, default):
                if group_speed is not None:
                    pieces.append((chunk, group_speed, effects))
                else:
                    pieces.append((chunk, speed, tuple(effects) + (group_effect,)))

            if pending and len(pieces) > base:
                head, speed, effects = pieces[base]
                pieces[base] = (head, speed, tuple(pending) + tuple(effects))
                pending = []

            position = end
            chain = False
            continue

        match = item
        chunk = text[position : match.start()].strip(" \u3000")
        number, code, digits = match.group(1), match.group(2), match.group(3)

        if code is not None:
            count = int(digits) if digits else 1
            effect = (
                code.lower(),
                float(min(max(count, 1), 20)),
            )

            if chunk:
                pieces.append((chunk, default, tuple(pending + [effect])))
                pending = []
                chain = True
            elif chain and pieces:
                head, speed, effects = pieces[-1]
                pieces[-1] = (head, speed, tuple(effects) + (effect,))
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
                pieces.append((chunk, max(0.1, min(100.0, speed)), tuple(pending)))

            pending = []
            chain = False

        position = match.end()

    tail = text[position:].strip(" \u3000")

    if tail:
        pieces.append((tail, default, tuple(pending)))
    elif pending and pieces:
        head, speed, effects = pieces[-1]
        pieces[-1] = (head, speed, tuple(effects) + tuple(pending))

    return pieces


def _apply_trailing_speed(pieces, speed):
    """文末の速度指定を、最後のピース（＝直前の区間全体）に掛ける。

    文末の ``f2`` は _parse_commands が剥がして渡ってくる。区間の途中に
    トークンが無ければ、その区間＝ここまでの本文すべてになる。1 語だけに
    掛けたいときは ``(その語)f2`` と括弧で囲む。
    """
    if not pieces or speed is None:
        return pieces

    chunk, _current, effects = pieces[-1]
    pieces[-1] = (chunk, speed, effects)

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
