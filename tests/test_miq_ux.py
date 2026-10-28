# ruff: noqa: E501, F841, B905
"""miq のコマンド（miqmiq / miq12 / miq 縦 / miq vs / miq台帳）のテスト。"""

import asyncio
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jllm_plugins.miq import find_layout
from jllm_plugins.miq.cog import MiqCog
from jllm_plugins.miq.command import Command, parse, usage
from jllm_plugins.miq.gather import conversation, same_author
from jllm_plugins.restrictions.service import UserRestrictions
from PIL import Image

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def avatar_png(size=256, color=(30, 90, 140)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")

    return buffer.getvalue()


class Author:
    def __init__(self, ident=1, name="koshian", display="こしあん", data=None):
        self.id = ident
        self.name = name
        self.display_name = display
        self.bot = False
        self.display_avatar = Asset(data or avatar_png())
        self.guild = None


class Asset:
    def __init__(self, data):
        self.data = data

    def replace(self, **kwargs):
        return self

    async def read(self):
        return self.data


class Attachment:
    def __init__(self, data=None, content_type="image/png", filename="shot.png", spoiler=False):
        self.data = data if data is not None else avatar_png(64)
        self.content_type = content_type
        self.size = len(self.data)
        self.filename = filename
        self._spoiler = spoiler

    def is_spoiler(self):
        return self._spoiler

    async def read(self):
        return self.data


class Msg:
    def __init__(self, content="", *, author=None, ident=1, when=None,
                 attachments=(), reply_to=None, channel=None):
        self.content = content
        self.author = author or Author()
        self.id = ident
        self.created_at = when or T0 + timedelta(minutes=ident)
        self.attachments = list(attachments)
        self.type = SimpleNamespace(name="default")
        self.guild = SimpleNamespace(id=1)
        self.mentions = []
        self.replies = []
        self.channel = channel or self
        self.reference = None

        if reply_to is not None:
            self.reference = SimpleNamespace(resolved=reply_to, message_id=reply_to.id)

    async def reply(self, content=None, **kwargs):
        self.replies.append({"content": content, **kwargs})

    def typing(self):
        message = self

        class Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return Typing()


class Channel:
    """古い順のメッセージを持つ。history は新しい順に返す。"""

    def __init__(self):
        self.messages = []

    def add(self, message):
        message.channel = self
        self.messages.append(message)

        return message

    def typing(self):
        class Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return Typing()

    def history(self, limit=None, before=None):
        try:
            index = self.messages.index(before)
        except ValueError:
            index = len(self.messages)

        subset = list(reversed(self.messages[:index]))

        if limit is not None:
            subset = subset[:limit]

        async def generate():
            for message in subset:
                yield message

        return generate()


class ParseTests(unittest.TestCase):
    def test_commands(self):
        cases = {
            "miq": Command("quote"),
            "MIQ": Command("quote"),
            "miqmiq": Command("quote", count=2),
            "miqmiqmiq": Command("quote", count=3),
            "miq12": Command("quote", layout=12),
            "miqmiq3": Command("quote", count=2, layout=3),
            "miq 12": Command("quote", layout=12),
            "miq 縦": Command("quote", orientation="portrait"),
            "miq 正方形": Command("quote", orientation="square"),
            "miq vs": Command("quote", mood="vs"),
            "miq vs 縦": Command("quote", mood="vs", orientation="portrait"),
            "miqmiq 縦 cute": Command("quote", count=2, orientation="portrait", category="cute"),
            "miq台帳": Command("catalog"),
            "miq 一覧": Command("catalog"),
            "miq help": Command("help"),
            "Ｍｉｑ": Command("quote"),
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse(text), expected)

    def test_plain_chat_is_not_a_command(self):
        for text in ("", "miq!", "miqu", "miq ってなに？", "miq して", "miq miq", "みく"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_words_can_be_changed(self):
        self.assertEqual(parse("quote12", ("quote",)), Command("quote", layout=12))
        self.assertIsNone(parse("miq", ("quote",)))

    def test_usage_mentions_the_word(self):
        self.assertIn("miq台帳", usage())


class GatherTests(unittest.IsolatedAsyncioTestCase):
    def test_same_author_skips_other_people(self):
        channel = Channel()
        a1 = channel.add(Msg("a1", author=Author(1), ident=1))
        channel.add(Msg("b1", author=Author(2), ident=2))
        a2 = channel.add(Msg("a2", author=Author(1), ident=3))
        a3 = channel.add(Msg("a3", author=Author(1), ident=4))

        def history(anchor):
            return channel.history(before=anchor)

        picked = asyncio.run(same_author(a3, 3, history))

        self.assertEqual([m.content for m in picked], ["a1", "a2", "a3"])

    def test_conversation_uses_the_replied_message(self):
        channel = Channel()
        b1 = channel.add(Msg("b1", author=Author(2), ident=1))
        b2 = channel.add(Msg("b2", author=Author(2), ident=2))
        a1 = channel.add(Msg("a1", author=Author(1), ident=3))
        a2 = channel.add(Msg("a2", author=Author(1), ident=4, reply_to=b2))

        def history(anchor):
            return channel.history(before=anchor)

        async def replied_to(message):
            return message.reference.resolved if message.reference else None

        theirs, mine = asyncio.run(conversation(a2, history, replied_to))

        self.assertEqual([m.content for m in mine], ["a1", "a2"])
        self.assertEqual([m.content for m in theirs], ["b1", "b2"])


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.history = self.root / "miq.jsonl"
        self.restrictions = UserRestrictions(self.root / "restrictions.json")
        self.cog = MiqCog(
            bot=SimpleNamespace(restrictions=self.restrictions),
            history_path=self.history,
            emoji_dir=self.root / "emojis",
        )

    def lines(self):
        if not self.history.is_file():
            return []

        return [json.loads(line) for line in self.history.read_text(encoding="utf-8").splitlines()]

    async def run_with(self, text, target=None, **kwargs):
        channel = kwargs.pop("channel", None) or Channel()
        target = target if target is not None else Msg("引用される本文", author=Author())
        channel.add(target)
        command_message = channel.add(Msg(text, author=Author(9), ident=90, reply_to=target))
        await self.cog.on_message(command_message)

        return command_message

    async def test_catalog_needs_no_reply(self):
        pages = [("miq-layouts-1.png", b"one"), ("miq-layouts-2.png", b"two")]

        with mock.patch("jllm_plugins.miq.cog.miq_catalog.pages", lambda: pages):
            message = await self.run_with("miq台帳")

        self.assertEqual(len(message.replies), 1)
        files = message.replies[0]["files"]
        self.assertEqual([f.filename for f in files], ["miq-layouts-1.png", "miq-layouts-2.png"])

    async def test_help_needs_no_reply(self):
        message = await self.run_with("miq help")

        self.assertIn("miq台帳", message.replies[0]["content"])

    async def test_layout_number_goes_to_compose(self):
        seen = {}

        def fake_compose(avatar, text, name="", handle="", *args, **kwargs):
            seen.update(kwargs)
            chosen = kwargs["layout"]

            if isinstance(chosen, int):
                chosen = find_layout(chosen)

            return Image.new("RGBA", (640, 360)), chosen, "code"

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            message = await self.run_with("miq18")

        self.assertEqual(seen["layout"], 18)
        self.assertIn("#18", message.replies[0]["content"])

    async def test_unknown_layout_number_is_reported(self):
        message = await self.run_with("miq99")

        self.assertEqual(message.replies[0]["content"], "構図 99 番はありません（`miq台帳` で一覧）。")

    async def test_portrait_uses_the_portrait_size(self):
        seen = {}

        def fake_compose(avatar, text, name="", handle="", font_file=None,
                         bold_file=None, size=None, *args, **kwargs):
            seen["size"] = size

            return Image.new("RGBA", (540, 960)), "story", "gothic"

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_with("miq 縦")

        self.assertEqual(seen["size"], (540, 960))

    async def test_dialogue_uses_two_speakers(self):
        seen = {}

        def fake_dialogue(left, right, mood, **kwargs):
            seen["mood"] = mood
            seen["names"] = (left.name, right.name)

            return Image.new("RGBA", (640, 360)), mood, "dela"

        channel = Channel()
        other = channel.add(Msg("やあ", author=Author(2, display="あいて"), ident=1))
        target = channel.add(Msg("どうも", author=Author(1, display="じぶん"), ident=2, reply_to=other))

        with mock.patch("jllm_plugins.miq.cog.compose_dialogue", fake_dialogue):
            message = await self.run_with("miq vs", target=target, channel=channel)

        self.assertEqual(seen["mood"], "vs")
        self.assertEqual(seen["names"], ("あいて", "じぶん"))
        self.assertIn("対戦", message.replies[0]["content"])

    async def test_square_dialogue_is_refused(self):
        message = await self.run_with("miq vs 正方形")

        self.assertEqual(message.replies[0]["content"], "掛け合いは横か縦だけに対応しています。")

    async def test_repeating_the_word_merges_messages(self):
        seen = {}

        def fake_compose(avatar, text, *args, **kwargs):
            seen["text"] = text

            return Image.new("RGBA", (640, 360)), "classic", "gothic"

        channel = Channel()
        first = channel.add(Msg("ひとつめ", author=Author(1), ident=1))
        second = channel.add(Msg("ふたつめ", author=Author(1), ident=2))
        third = channel.add(Msg("みっつめ", author=Author(1), ident=3))

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_with("miqmiqmiq", target=third, channel=channel)

        self.assertEqual(seen["text"], "ひとつめ\nふたつめ\nみっつめ")

    async def test_attachments_become_a_collage(self):
        seen = {}

        def fake_compose(avatar, text, *args, **kwargs):
            seen["attachment"] = kwargs.get("attachment")

            return Image.new("RGBA", (640, 360)), "classic", "gothic"

        target = Msg("写真", author=Author(), attachments=[Attachment(), Attachment()])

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_with("miq", target=target)

        self.assertTrue(seen["attachment"])

    async def test_image_only_message_is_allowed(self):
        seen = {}

        def fake_compose(avatar, text, *args, **kwargs):
            seen["text"] = text

            return Image.new("RGBA", (640, 360)), "classic", "gothic"

        target = Msg("", author=Author(), attachments=[Attachment()])

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_with("miq", target=target)

        self.assertEqual(seen["text"], "（画像）")

    async def test_plain_chat_is_ignored(self):
        message = await self.run_with("miq ってなに？")

        self.assertEqual(message.replies, [])
        self.assertEqual(self.lines(), [])
