# ruff: noqa: E501, B007
"""これまでの会話を Discord から取り込み、会話セッション（SQLite）を作る。

セッション機能より前の会話は保存されていないので、Discord API から辿り直して
``data/sessions/`` に作り直す。**何度実行しても二重にはならない**
（登録済みのメッセージIDは飛ばす）。

x230 で実行する（トークンとセッションの実体がそこにあるため）::

    cd /home/ryu/jllm-voice
    .venv/bin/python tools/import-sessions.py --channel 1559571226622566441
    .venv/bin/python tools/import-sessions.py --channel 1552346936898752623 --limit 3000
    .venv/bin/python tools/import-sessions.py --jsonl data/exports/1552346936898752623/messages.jsonl
    .venv/bin/python tools/import-sessions.py --channel ... --dry-run

- 呼び出し（メンション／語尾の ``jbot``）で始まった会話を根として、
  Botの回答と、その回答への返信を順に辿る。返信した人が違えばフォーク。
- Botの回答が複数ページに分かれている場合は1つの回答にまとめる。
- 実行中でも Bot は止めなくてよい（SQLite は1操作ごとに開いて閉じる）。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402
from jllm_plugins.conversation.sessions import SessionStore  # noqa: E402

API = "https://discord.com/api/v10"
PAGE = 100


def normalize(raw: dict) -> dict | None:
    """REST/エクスポートの生メッセージを、取り込みに必要な形へ。"""
    if not isinstance(raw, dict):
        return None

    message_id = str(raw.get("id") or "")

    if not message_id:
        return None

    author = raw.get("author") or {}
    reference = raw.get("message_reference") or {}
    member = raw.get("member") or {}

    name = (member.get("nick") or author.get("global_name")
            or author.get("username") or "")

    return {
        "id": message_id,
        "author_id": str(author.get("id") or ""),
        "author_name": str(name),
        "author_avatar": str(author.get("avatar") or ""),
        "at": str(raw.get("timestamp") or ""),
        "is_bot": bool(author.get("bot")),
        "webhook": bool(raw.get("webhook_id")),
        "content": str(raw.get("content") or ""),
        "reply_to": str(reference.get("message_id") or "") or None,
    }


def is_call(message: dict, bot_id: str, suffix: str) -> bool:
    """その投稿が会話の呼び出し（メンション／語尾の合図）か。"""
    content = message["content"]

    if f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content:
        return True

    return bool(suffix) and content.rstrip().casefold().endswith(suffix.casefold())


def call_text(message: dict, bot_id: str, suffix: str) -> str:
    text = message["content"]

    for token in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        text = text.replace(token, "")

    stripped = text.rstrip()

    if suffix and stripped.casefold().endswith(suffix.casefold()):
        text = stripped[: -len(suffix)]

    return text.strip()


def import_messages(messages, store: SessionStore, bot_id: str, guild_id, channel_id,
                    suffix="", model="", dry_run=False) -> dict:
    """古い順のメッセージ列をセッションへ再構築する。

    返り値は集計（scanned / sessions / turns / skipped）。
    """
    result = {"scanned": 0, "sessions": 0, "turns": 0, "skipped": 0}
    pending: dict[str, list[dict]] = {}
    # 表示名とアイコンは、登録済みの投稿からも拾っておく（後から補うため）。
    profiles: dict[str, tuple] = {}

    def flush():
        """溜めたBotの回答を、返信先の会話へ1つの回答として入れる。"""
        for target_id, answers in pending.items():
            owner = store.message(target_id)

            if owner is None:
                continue

            text = "\n\n".join(answer["content"] for answer in answers if answer["content"])

            if not text:
                continue

            if dry_run:
                result["turns"] += 1
                continue

            turn = store.append_assistant(
                owner["session_id"], text, answers[0]["id"], created_at=answers[0]["at"],
            )

            for answer in answers:
                store.register_message(
                    answer["id"], owner["session_id"], turn, True, created_at=answer["at"],
                )

            result["turns"] += 1

        pending.clear()

    for raw in messages:
        message = normalize(raw)

        if message is None or message["webhook"]:
            continue

        result["scanned"] += 1

        if not message["is_bot"] and message["author_id"]:
            name, avatar = profiles.get(message["author_id"], ("", ""))
            profiles[message["author_id"]] = (
                message["author_name"] or name,
                message["author_avatar"] or avatar,
            )

        if message["is_bot"]:
            # Botの回答。返信先（＝ユーザーの呼び出し）ごとにまとめる。
            # 取り込み済みなら何もしない（二度実行しても増やさない）。
            if store.message(message["id"]) is not None:
                result["skipped"] += 1
            elif message["reply_to"]:
                pending.setdefault(message["reply_to"], []).append(message)
            continue

        flush()

        if store.message(message["id"]) is not None:
            result["skipped"] += 1
            continue

        if is_call(message, bot_id, suffix):
            reply_to = None
            text = call_text(message, bot_id, suffix)
        elif message["reply_to"] and store.message(message["reply_to"]) is not None:
            reply_to = message["reply_to"]
            text = message["content"]
        else:
            result["skipped"] += 1
            continue

        if dry_run:
            result["sessions"] += 1
            continue

        session_id, _action = store.resolve(
            guild_id, channel_id, message["author_id"], message["author_name"], reply_to,
        )

        if reply_to is None:
            store.update_settings(session_id, model=model)

        store.append_user(
            session_id, message["author_id"], message["author_name"],
            text, message["id"], created_at=message["at"], avatar=message["author_avatar"],
        )
        result["sessions"] += 1 if reply_to is None else 0
        result["turns"] += 1

    flush()

    if not dry_run:
        for user_id, (name, avatar) in profiles.items():
            store.remember_user(user_id, name, avatar)

    result["users"] = len(profiles)
    return result


class Fetcher:
    """Discord REST（読み取りのみ）。"""

    def __init__(self, token, session):
        self.token = token
        self.session = session

    async def get(self, path, **params):
        headers = {"Authorization": "Bot " + self.token}

        for attempt in range(4):
            async with self.session.get(API + path, headers=headers, params=params) as response:
                if response.status == 429:
                    wait = float(response.headers.get("Retry-After", "1") or 1)
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                return await response.json()

        raise RuntimeError("Discord API が混み合っています: " + path)

    async def messages(self, channel_id, limit):
        collected, before = [], None

        while len(collected) < limit:
            params = {"limit": PAGE}

            if before:
                params["before"] = before

            batch = await self.get(f"/channels/{channel_id}/messages", **params)

            if not batch:
                break

            collected.extend(batch)
            before = batch[-1]["id"]
            print(f"  取得 {len(collected)}件…", flush=True)

        collected.sort(key=lambda item: int(item["id"]))
        return collected[:limit]


async def run(args):
    store = SessionStore(os.environ.get("SESSION_DIR", "data/sessions"))
    suffix = os.environ.get("TALK_CALL_SUFFIX", "")
    model = os.environ.get("TALK_MODEL", "")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    channel_id = args.channel or ""
    guild_id = args.guild or ""
    bot_id = args.bot_id or ""

    if args.jsonl:
        # エクスポートは古い順なので、そのまま流し読みする（大量でもメモリを使わない）。
        def rows():
            with Path(args.jsonl).open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)

        messages = rows()
        print(f"{args.jsonl}: 読み込み")

        if not token and (not bot_id or not guild_id):
            raise SystemExit("BotのIDとギルドIDを--bot-id/--guildで指定するか、.env を用意してください")
    else:
        if not token:
            raise SystemExit("DISCORD_TOKEN がありません（.env を確認）")

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            fetcher = Fetcher(token, session)
            messages = await fetcher.messages(channel_id, args.limit)

            if not bot_id or not guild_id:
                bot = await fetcher.get("/users/@me")
                bot_id = bot_id or str(bot["id"])

                if not guild_id:
                    guilds = await fetcher.get("/users/@me/guilds")
                    guild_id = str(guilds[0]["id"]) if guilds else ""

        print(f"channel={channel_id} guild={guild_id} bot={bot_id} 取得={len(messages)}件")

    if not channel_id:
        raise SystemExit("チャンネルIDが必要です（--channel）")

    if not bot_id and token:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            bot_id = str((await Fetcher(token, session).get("/users/@me"))["id"])

    if not bot_id:
        raise SystemExit("BotのIDが分かりません（--bot-id を指定）")

    result = import_messages(
        messages, store, bot_id, guild_id, channel_id,
        suffix=suffix, model=model, dry_run=args.dry_run,
    )
    if args.fix_times and not args.dry_run:
        fixed = store.repair_timestamps(channel_id)
        print("時刻を直した: 発言{}件 / セッション{}件".format(fixed["turns"], fixed["sessions"]))

    if not args.dry_run:
        filled = store.backfill_titles(channel_id)

        if filled:
            print(f"名前を補ったセッション: {filled}件")

    print("結果:", json.dumps(result, ensure_ascii=False))
    print("セッション:", store.for_channel_summary(guild_id, channel_id) or "（なし）")


def main():
    parser = argparse.ArgumentParser(description="これまでの会話をセッションへ取り込む")
    parser.add_argument("--channel", help="対象チャンネルID")
    parser.add_argument("--guild", help="ギルドID（省略時はAPIから取得）")
    parser.add_argument("--bot-id", help="BotのユーザーID（省略時はAPIから取得）")
    parser.add_argument("--limit", type=int, default=1000, help="取得するメッセージ数（既定1000）")
    parser.add_argument("--jsonl", help="エクスポート済み messages.jsonl を使う（RESTを叩かない）")
    parser.add_argument("--dry-run", action="store_true", help="数えるだけ（書き込まない）")
    parser.add_argument("--fix-times", action="store_true",
                        help="既存の記録の時刻をメッセージIDから復元して直す")
    args = parser.parse_args()

    if not args.channel and not args.jsonl:
        parser.error("--channel か --jsonl が必要です")

    asyncio.run(run(args))


if __name__ == "__main__":
    started = time.monotonic()
    main()
    print(f"所要 {time.monotonic() - started:.1f}秒")
