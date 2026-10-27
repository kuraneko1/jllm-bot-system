"""miq まわりの共通判定。

- :func:`is_miq_trigger`: miq のコマンドか（読み上げ・会話から外す）
- :func:`is_miq_image`: miq が作った画像の投稿か（この投稿への返信には反応しない）

判定を1か所にまとめて、機能ごとにズレないようにしている。
"""

from .command import parse

MIQ_WORDS = ("miq",)

#: 生成する画像のファイル名の頭（この名前で「miqが作った画像」を見分ける）
MIQ_FILE_PREFIX = "miq-"


def is_miq_trigger(content: str, words=MIQ_WORDS) -> bool:
    """本文が miq のコマンドか（``miq`` / ``miqmiq3 縦`` / ``miq vs`` / ``miq台帳`` …）。

    知らない語が付いた文（``miq ってなに？``）はコマンドではないので、通常どおり
    読み上げ・会話の対象になる。
    """

    return parse(content, words) is not None


def is_miq_image(message) -> bool:
    """その投稿が miq の生成した画像かどうか（bot の ``miq-*.png``）。

    この投稿への返信には、内容によらず反応しない（会話も引用画像も作らない）。
    """

    author = getattr(message, "author", None)

    if not getattr(author, "bot", False):
        return False

    return any(
        str(getattr(attachment, "filename", "")).startswith(MIQ_FILE_PREFIX)
        for attachment in (getattr(message, "attachments", None) or [])
    )
