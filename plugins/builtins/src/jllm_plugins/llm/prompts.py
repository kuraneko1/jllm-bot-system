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


def style_of(client) -> str:
    """クライアントの system ロールの扱い（``system`` / ``plain``）。"""

    return getattr(client, "prompt_style", SYSTEM)


def uses_plain(client) -> bool:
    return style_of(client) == PLAIN


def chat_messages(client, system, user):
    """会話1往復。``plain`` では system を送らない（人格は既定でLoRA側）。"""

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


def msg_user(messages) -> str:
    """最後の user メッセージの本文。"""

    return str(messages[user_index(messages)].get("content") or "")


def set_user(messages, content):
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
