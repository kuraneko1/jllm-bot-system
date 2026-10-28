# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""どのメッセージを1枚にまとめるか。

- same_author: miqmiq… = 返信先の人と、その少し前の発言（間の他人は飛ばす）
- conversation: miq vs / love / talk = 2人。それぞれの連続した発言をまとめる

Discord のオブジェクトは author.id / created_at / content / type / attachments しか
読まないので、テストでは普通のオブジェクトを渡せる。history(message) は
「その投稿より前のメッセージ」を新しい順に返す非同期イテレータ
（channel.history(limit=..., before=message)）。
"""

from __future__ import annotations

from datetime import timedelta

from jllm_plugins.miq.command import parse

#: miqmiq… でさかのぼる件数と時間の上限
SCAN_LIMIT = 100
WINDOW = timedelta(hours=1)
#: 連続した発言とみなす間隔と、まとめる最大件数
RUN_GAP = timedelta(minutes=10)
RUN_MAX = 8


def is_regular(message) -> bool:
    kind = getattr(getattr(message, "type", None), "name", "default")

    return kind in ("default", "reply")


def has_content(message) -> bool:
    return bool(
        (message.content or "").strip()
        or getattr(message, "attachments", None)
        or getattr(message, "stickers", None)
        or getattr(message, "embeds", None)
    )


def usable(message) -> bool:
    """引用する価値のある投稿（システム行・miq のコマンド・空は除く）。"""

    return is_regular(message) and has_content(message) and parse(message.content) is None


async def same_author(target, count, history):
    """返信先と、同じ人の直前の発言（最大 count 件・古い順）。"""

    picked = [target]

    if count > 1:
        async for message in history(target):
            if target.created_at - message.created_at > WINDOW:
                break
            if message.author.id == target.author.id and usable(message):
                picked.append(message)

                if len(picked) >= count:
                    break

    return picked[::-1]


async def run(anchor, history):
    """anchor と同じ人の連続した発言をまとめる。

    戻り値は「古い順のメッセージ」と「直前に話していた相手の投稿」。
    """

    picked = [anchor]
    previous = None

    async for message in history(anchor):
        if message.author.id != anchor.author.id:
            if usable(message):
                previous = message
                break
            if parse(message.content) is not None:
                continue
            break

        if picked[-1].created_at - message.created_at > RUN_GAP:
            break
        if not usable(message):
            continue

        picked.append(message)

        if len(picked) >= RUN_MAX:
            break

    return picked[::-1], previous


async def conversation(target, history, replied_to):
    """掛け合いの「相手」と「返信先」を古い順で返す（作れなければ None）。

    相手は返信先が返信している投稿。無ければ「返信先の直前に話していた人」。
    """

    mine, previous = await run(target, history)
    other = await replied_to(target)

    if other is None or other.author.id == target.author.id or not usable(other):
        other = previous
    if other is None:
        return None

    theirs, _ = await run(other, history)

    return theirs, mine
