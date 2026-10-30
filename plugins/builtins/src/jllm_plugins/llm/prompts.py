"""バックエンドごとのプロンプトの組み方（system ロールを送れるか）。

``gemma4-atmos-sft``（z170 / ``/llm switch atmos``）のチャットテンプレートは
**system ロールを ``<|turn>model`` として描く**。つまりシステムプロンプトは
「アシスタントがそう言った」という発言として文脈に入る。そのため:

- 長い「あなたは〜です」を送ると、人格（LoRA の #雑談vc 口調）ではなく
  汎用アシスタントの口調に寄る。実測でも
  「おめでとうございます！何世代くらいですか？…用途は何になりますか？」のように
  丁寧な逆質問になった（system 無しなら「おお」）。
- 会話ログを JSON で渡すと、資料内の指示として扱われて暴走することがある。
  平文の「名前: 本文」で並べた方が素直に続く。
- 翻訳・要約のように**指示が要る**用途は、その指示を system ではなく
  ユーザーの発言の先頭に置く（指示の文面は変えない）。

そのため :data:`PLAIN` のバックエンドには system を送らず、指示は user に畳む。
"""

from __future__ import annotations

SYSTEM = "system"
PLAIN = "plain"

#: 画像1枚あたりの固定トークン数。llama.cpp の mmproj は画像を数百トークンへ
#: 畳むので、base64 の文字数（数百万）ではなくこの値で見積もる。
IMAGE_TOKENS = 768


def style_of(client) -> str:
    """クライアントの system ロールの扱い（``system`` / ``plain``）。"""

    return getattr(client, "prompt_style", SYSTEM)


def uses_plain(client) -> bool:
    return style_of(client) == PLAIN


def chat_messages(client, system, user):
    """会話1往復。``plain`` では system を送らない（人格は既定でLoRA側）。

    ``user`` は文字列でも OpenAI の content parts（画像付き）でもよい。
    parts の場合はそのまま user ターンへ渡す（画像対応バックエンドだけが使う）。
    """

    if uses_plain(client):
        return [{"role": "user", "content": user}]

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def instructed_messages(client, instruction, content):
    """指示＋本文（翻訳・要約）。

    ``plain`` のバックエンドは system を送れないので、**同じ指示を本文の先頭**に置く
    （指示そのものは変えない）。
    """

    if uses_plain(client):
        return [{"role": "user", "content": instruction.rstrip() + "\n\n" + content}]

    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": content},
    ]


def user_index(messages) -> int:
    """最後の user メッセージの位置（``plain`` では 0 のことがある）。"""

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index

    return len(messages) - 1


def content_size(content):
    """content の概算文字数と画像枚数を返す。

    content parts の ``image_url`` は base64 を数えず、画像1枚として数える。
    文字列をそのまま渡したときは従来どおり「文字数, 0」になる。
    """

    if not isinstance(content, list):
        return len(str(content or "")), 0

    chars = images = 0

    for part in content:
        if not isinstance(part, dict):
            chars += len(str(part))
        elif part.get("type") == "image_url":
            images += 1
        else:
            chars += len(str(part.get("text") or ""))

    return chars, images


def content_text(content) -> str:
    """content parts のうちテキストだけを連結する（会話ログ・圧縮の入力用）。"""

    if not isinstance(content, list):
        return str(content or "")

    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") != "image_url"
    )


def msg_user(messages) -> str:
    """最後の user メッセージの本文（画像 parts は落とす）。"""

    return content_text(messages[user_index(messages)].get("content"))


def set_user(messages, content):
    """user ターンの本文を差し替える。

    content parts（画像付き）を渡すと**テキストごと差し替え**になり、画像は
    落ちる。長文の圧縮では画像を送り直せないので、呼び出し側も文字列を渡す。
    """

    messages[user_index(messages)]["content"] = content
    return messages


def discord_log(records) -> str:
    """会話ログを「名前: 本文」の平文にする（JSON より素直に続く）。"""

    lines = []

    for record in records:
        text = str(record.get("text") or "").strip()

        if not text:
            continue

        lines.append("{}: {}".format(record.get("author") or "?", text))

    return "\n".join(lines)
