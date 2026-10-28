# ruff: noqa: E501, F841, B905
"""返信に ``miq`` と書いたときの引用画像（cogs/miq.py）。"""
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jllm_plugins.miq import (
    AUTO_LAYOUTS,
    LAYOUT_WEIGHTS,
    LAYOUTS,
    choose_layout,
    render,
    resolve_align,
    resolve_size,
    text_region,
)
from jllm_plugins.miq.cog import MiqCog, icon_size, is_miq_image, resolve_mentions
from jllm_plugins.miq.miq_fonts import FONT_DIR, FONT_FILES
from jllm_plugins.miq.miq_words import CUSTOM, GRAPHEME, units
from jllm_plugins.miq.trigger import is_miq_trigger
from jllm_plugins.restrictions.service import UserRestrictions
from PIL import Image, ImageDraw


def fonts_available() -> bool:
    """同梱フォント（tools/setup_miq_fonts.py で取得）が揃っているか。"""

    return all((FONT_DIR / name).is_file() for name in FONT_FILES.values())


def require_fonts(test):
    if not fonts_available():
        test.skipTest("同梱フォントが無い環境（tools/setup_miq_fonts.py で取得）")

    return test


def avatar_png(size=256, color=(30, 90, 140)) -> bytes:
    image = Image.new("RGB", (size, size), color)
    ImageDraw.Draw(image).ellipse((size // 6, size // 6, size * 5 // 6, size * 5 // 6), fill=(230, 235, 245))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return buffer.getvalue()


class TriggerTests(unittest.TestCase):
    def test_only_the_word_itself_fires(self):
        self.assertTrue(is_miq_trigger("miq"))
        self.assertTrue(is_miq_trigger("MIQ"))
        self.assertTrue(is_miq_trigger("  miq  "))
        self.assertTrue(is_miq_trigger("Ｍｉｑ"))  # 全角も受ける（NFKCで正規化）

    def test_other_text_does_not_fire(self):
        for content in ("", "miq!", "miqu", "miq miq", "miq して", "m i q", "みく"):
            with self.subTest(content=content):
                self.assertFalse(is_miq_trigger(content))

    def test_words_can_be_changed(self):
        self.assertTrue(is_miq_trigger("quote", ("quote", "miq")))
        self.assertFalse(is_miq_trigger("miq", ("quote",)))


class FakeAsset:
    """discord.py と同じく、2の累乗以外の size は ValueError にする。"""

    def __init__(self, data):
        self.data = data
        self.requested = None

    def replace(self, **kwargs):
        size = kwargs.get("size")

        if size is not None and (size < 16 or size > 4096 or size & (size - 1)):
            raise ValueError("size must be a power of 2 between 16 and 4096")

        self.requested = kwargs

        return self

    async def read(self):
        return self.data


class FakeAuthor:
    def __init__(self, name="こしあん", display="こしあん", data=None):
        self.name = name
        self.display_name = display
        self.id = 12345
        self.bot = False
        self.display_avatar = FakeAsset(data if data is not None else avatar_png())


class FakeMessage:
    def __init__(self, content="", *, author=None, reference=None, ident=1, attachments=()):
        self.content = content
        self.attachments = list(attachments)
        self.author = author or FakeAuthor()
        self.reference = reference
        self.id = ident
        self.guild = SimpleNamespace(id=1)
        self.channel = self
        self.replies = []
        self.typing_used = False

    async def reply(self, content=None, **kwargs):
        self.replies.append({"content": content, **kwargs})

    async def fetch_message(self, message_id):
        raise AssertionError("キャッシュ済みの返信先を取りに行ってはいけない")

    def typing(self):
        message = self

        class Typing:
            async def __aenter__(self):
                message.typing_used = True

            async def __aexit__(self, *exc):
                return False

        return Typing()


class Reference:
    def __init__(self, resolved=None, message_id=99):
        self.resolved = resolved
        self.message_id = message_id


class ServerEmojiTests(unittest.TestCase):
    """サーバー絵文字（<:name:id>）を画像として貼れる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "123.png"
        # 目立つ色の四角をサーバー絵文字の画像に見立てる
        Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(self.path)

    def test_token_is_a_single_unit(self):
        # 日本語の連なりはまとまり、サーバー絵文字は1単位になる
        self.assertEqual(units("やった<:koshian:123>だ"), ["やった", "<:koshian:123>", "だ"])
        self.assertEqual(CUSTOM.findall("<a:cat:456>"), [("cat", "456")])

    def test_image_is_loaded_from_the_mapping(self):
        from jllm_plugins.miq.miq_text import EmojiImages

        image = EmojiImages(None, {"123": str(self.path)}).get("<:koshian:123>")

        self.assertIsNotNone(image)
        self.assertGreater(image.width, 0)

    def test_missing_image_is_none(self):
        from jllm_plugins.miq.miq_text import EmojiImages

        self.assertIsNone(EmojiImages(None, {"123": str(self.path)}).get("<:koshian:999>"))
        self.assertIsNone(EmojiImages(None, None).get("<:koshian:999>"))

    def test_render_pastes_the_server_emoji(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            output = Path(tmp) / "quote.png"

            # 構図は既定で毎回ランダム（横27種）。縦書き（tategaki）だけは絵文字を
            # 縦に置けないので、seed で構図を固定して結果を再現できるようにする。
            render(avatar, "テスト<:koshian:123>です", output, "名前", "handle",
                   custom_emojis={"123": str(self.path)}, seed=1)

            with Image.open(output).convert("RGB") as image:
                pixels = image.load()
                greens = [
                    (x, y) for x in range(image.width) for y in range(image.height)
                    if pixels[x, y][1] > 200 and pixels[x, y][0] < 100 and pixels[x, y][2] < 100
                ]

        self.assertTrue(greens, "サーバー絵文字の画像が貼られていない")



class MentionTests(unittest.IsolatedAsyncioTestCase):
    """本文のメンションを、そのサーバーで見えている名前に置き換える。"""

    class Guild:
        def get_member(self, ident):
            member = FakeAuthor(name="koshian", display=f"サーバー名{ident}")
            member.id = ident

            return member

    async def test_mention_becomes_the_server_name(self):
        target = FakeMessage("")
        target.guild = self.Guild()
        target.mentions = [FakeAuthor(name="koshian", display="グローバル名")]

        target.mentions[0].id = 123
        result = await resolve_mentions("やった<@123>すごい", target)

        self.assertEqual(result, "やった@サーバー名123すごい")

    async def test_nickname_style_mention_is_also_replaced(self):
        target = FakeMessage("")
        target.guild = self.Guild()
        mentioned = FakeAuthor(name="koshian", display="グローバル名")
        mentioned.id = 456
        target.mentions = [mentioned]

        self.assertEqual(await resolve_mentions("<@!456> おつ", target), "@サーバー名456 おつ")

    async def test_unknown_mention_stays_as_is(self):
        target = FakeMessage("")
        target.guild = self.Guild()
        target.mentions = []

        self.assertEqual(await resolve_mentions("<@999> だれ", target), "<@999> だれ")

    async def test_text_without_mentions_is_unchanged(self):
        target = FakeMessage("")
        target.guild = self.Guild()
        target.mentions = []

        self.assertEqual(await resolve_mentions("ふつうの本文", target), "ふつうの本文")


class MiqImageTests(unittest.TestCase):
    """生成済みの画像かどうかの判定。"""

    def message(self, *, bot=True, filename="miq-123.png"):
        author = FakeAuthor()
        author.bot = bot
        attachment = SimpleNamespace(filename=filename)

        return SimpleNamespace(author=author, attachments=[attachment] if filename else [])

    def test_bot_image_with_the_miq_prefix_is_recognized(self):
        self.assertTrue(is_miq_image(self.message()))
        self.assertTrue(is_miq_image(self.message(filename="miq-999.png")))

    def test_other_attachments_are_not_miq_images(self):
        self.assertFalse(is_miq_image(self.message(filename="photo.png")))
        self.assertFalse(is_miq_image(self.message(filename="")))
        self.assertFalse(is_miq_image(SimpleNamespace(author=FakeAuthor(), attachments=None)))

    def test_a_users_image_is_not_treated_as_miq(self):
        self.assertFalse(is_miq_image(self.message(bot=False)))


class MiqCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = Path(self.tmp.name) / "miq.jsonl"
        self.restrictions = UserRestrictions(Path(self.tmp.name) / "restrictions.json")
        self.emoji_dir = Path(self.tmp.name) / "emojis"
        self.cog = MiqCog(
            bot=SimpleNamespace(restrictions=self.restrictions),
            history_path=self.history,
            emoji_dir=self.emoji_dir,
        )

    def history_lines(self):
        if not self.history.is_file():
            return []

        return [json.loads(line) for line in self.history.read_text(encoding="utf-8").splitlines()]

    async def run_message(self, message):
        await self.cog.on_message(message)

        return message

    async def test_reply_with_miq_posts_the_quote_image(self):
        require_fonts(self)
        target = FakeMessage("AIが人類を超える日は近い。", author=FakeAuthor(display="AM09:21", name="AM09_21"))
        message = FakeMessage("miq", reference=Reference(target))

        await self.run_message(message)

        self.assertEqual(len(message.replies), 1)
        sent = message.replies[0]
        self.assertTrue(sent["content"].startswith("-# #"))  # どの構図かを添える
        self.assertFalse(sent["mention_author"])
        attachment = sent["file"]
        self.assertTrue(attachment.filename.startswith("miq-"))
        attachment.fp.seek(0)
        with Image.open(io.BytesIO(attachment.fp.read())) as image:
            self.assertEqual(image.size, (640, 360))

    async def test_without_a_reply_it_explains_how_to_use(self):
        message = FakeMessage("miq")

        await self.run_message(message)

        self.assertEqual(len(message.replies), 1)
        self.assertIn("返信", message.replies[0]["content"])

    async def test_other_messages_are_ignored(self):
        target = FakeMessage("本文", reference=Reference())
        message = FakeMessage("こんにちは", reference=Reference(target))

        await self.run_message(message)

        self.assertEqual(message.replies, [])

    async def test_target_without_text_is_reported(self):
        target = FakeMessage("", reference=None)
        message = FakeMessage("miq", reference=Reference(target))

        await self.run_message(message)

        self.assertIn("本文がありません", message.replies[0]["content"])

    async def test_too_long_text_is_reported(self):
        target = FakeMessage("あ" * 5000)
        message = FakeMessage("miq", reference=Reference(target))

        await self.run_message(message)

        self.assertIn("長すぎます", message.replies[0]["content"])

    async def test_the_same_message_is_not_processed_twice(self):
        target = FakeMessage("一度だけ", author=FakeAuthor())
        message = FakeMessage("miq", reference=Reference(target), ident=777)

        await self.run_message(message)
        await self.run_message(message)

        self.assertEqual(len(message.replies), 1)

    async def test_success_is_recorded_in_the_history(self):
        require_fonts(self)
        target = FakeMessage("引用される本文", author=FakeAuthor(name="koshian", display="こしあん"))
        message = FakeMessage("miq", reference=Reference(target), ident=11)
        message.author.name = "requester"
        message.author.id = 999

        await self.run_message(message)

        entry = self.history_lines()[0]
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["target"], "koshian")
        self.assertEqual(entry["requester"], "requester")
        self.assertEqual(entry["chars"], len("引用される本文"))
        self.assertEqual(entry["size"], "640x360")
        self.assertEqual(entry["error"], "")

    async def test_failures_are_recorded_with_the_reason(self):
        message = FakeMessage("miq", reference=Reference(FakeMessage("")), ident=12)

        await self.run_message(message)

        entry = self.history_lines()[0]
        self.assertFalse(entry["ok"])
        self.assertIn("本文がありません", entry["error"])

    async def test_the_image_uses_the_server_visible_name(self):
        require_fonts(self)
        """グローバル表示名ではなく、そのサーバーでのニックネームを使う。"""

        class Guild:
            def get_member(self, ident):
                member = FakeAuthor(name="koshian", display="サーバーでの名前")
                member.id = ident

                return member

        target = FakeMessage("本文", author=FakeAuthor(name="koshian", display="グローバル表示名"))
        target.guild = Guild()
        message = FakeMessage("miq", reference=Reference(target), ident=21)
        captured = {}

        def fake_compose(avatar, text, name="", handle="", *args, **kwargs):
            captured["name"] = name
            captured["handle"] = handle

            return Image.new("RGBA", (640, 360)), "classic", "gothic"

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_message(message)

        self.assertEqual(captured["name"], "サーバーでの名前")
        self.assertEqual(captured["handle"], "koshian")
        self.assertEqual(len(message.replies), 1)

    async def test_image_uses_the_mentioned_name(self):
        require_fonts(self)

        class Guild:
            def get_member(self, ident):
                member = FakeAuthor(name="koshian", display="サーバー名123")
                member.id = ident

                return member

        target = FakeMessage("<@123> ありがとう", author=FakeAuthor(name="koshian", display="こしあん"))
        target.guild = Guild()
        mentioned = FakeAuthor(name="koshian", display="グローバル名")
        mentioned.id = 123
        target.mentions = [mentioned]
        message = FakeMessage("miq", reference=Reference(target), ident=41)
        captured = {}

        def fake_compose(avatar, text, *args, **kwargs):
            captured["text"] = text

            return Image.new("RGBA", (640, 360)), "classic", "gothic"

        with mock.patch("jllm_plugins.miq.cog.compose", fake_compose):
            await self.run_message(message)

        self.assertIn("@サーバー名123", captured["text"])

    async def test_replying_to_a_generated_image_does_nothing(self):
        bot_author = FakeAuthor(name="jllm-voice", display="jllm-voice")
        bot_author.bot = True
        image = FakeMessage("", author=bot_author, attachments=[SimpleNamespace(filename="miq-123.png")])
        message = FakeMessage("miq", reference=Reference(image), ident=31)

        await self.run_message(message)

        self.assertEqual(message.replies, [])            # 何も返さない
        entry = self.history_lines()[0]
        self.assertFalse(entry["ok"])
        self.assertIn("miq画像", entry["error"])

    async def test_replying_to_someone_elses_image_still_works(self):
        require_fonts(self)
        image = FakeMessage("", author=FakeAuthor(), attachments=[SimpleNamespace(filename="miq-123.png")])
        image.content = "画像の説明"
        message = FakeMessage("miq", reference=Reference(image), ident=32)

        await self.run_message(message)

        self.assertEqual(len(message.replies), 1)

    async def test_an_ignored_user_gets_no_image(self):
        author = FakeAuthor()
        self.restrictions.set_flag(1, author.id, "ignore", True)
        target = FakeMessage("引用される本文")
        message = FakeMessage("miq", author=author, reference=Reference(target), ident=41)

        await self.run_message(message)

        self.assertEqual(message.replies, [])
        self.assertEqual(self.history_lines(), [])

    async def test_cached_server_emoji_is_reused(self):
        self.emoji_dir.mkdir(parents=True, exist_ok=True)
        (self.emoji_dir / "123.png").write_bytes(avatar_png())

        found = await self.cog.custom_emojis("やった<:koshian:123>")

        self.assertEqual(found, {"123": str(self.emoji_dir / "123.png")})

    async def test_unknown_server_emoji_is_skipped(self):
        class Broken:
            closed = False

            def get(self, url):
                raise RuntimeError("オフライン")

        self.cog._session = Broken()

        found = await self.cog.custom_emojis("やった<:koshian:123>")

        self.assertEqual(found, {})

    async def test_bot_messages_are_ignored(self):
        author = FakeAuthor()
        author.bot = True
        message = FakeMessage("miq", author=author, reference=Reference(FakeMessage("x")))

        await self.run_message(message)

        self.assertEqual(message.replies, [])


class IconSizeTests(unittest.TestCase):
    """DiscordのアイコンURLは2の累乗しか受け付けない。"""

    def test_rounds_up_to_a_power_of_two(self):
        self.assertEqual(icon_size(431), 512)
        self.assertEqual(icon_size(300), 512)
        self.assertEqual(icon_size(970), 1024)
        self.assertEqual(icon_size(256), 256)

    def test_stays_within_the_allowed_range(self):
        self.assertEqual(icon_size(1), 16)
        self.assertEqual(icon_size(5000), 4096)

    def test_every_result_is_valid(self):
        for px in range(16, 4097, 7):
            size = icon_size(px)
            self.assertTrue(16 <= size <= 4096 and not size & (size - 1), px)


class SizeTests(unittest.TestCase):
    def test_default_is_small(self):
        with mock.patch.dict(os.environ, {"MIQ_SIZE": ""}):
            self.assertEqual(resolve_size(None), (640, 360))

    def test_explicit_sizes(self):
        self.assertEqual(resolve_size("1920x1080"), (1920, 1080))
        self.assertEqual(resolve_size("640x360"), (640, 360))
        self.assertEqual(resolve_size((1280, 720)), (1280, 720))

    def test_environment_default(self):
        with mock.patch.dict(os.environ, {"MIQ_SIZE": "1920x1080"}):
            self.assertEqual(resolve_size(None), (1920, 1080))

    def test_broken_size_is_reported(self):
        for value in ("wide", "854", "100x100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_size(value)


class WordTests(unittest.TestCase):
    """折り返しの単位（単語・絵文字・結合文字をまとめて扱う）。"""

    def test_custom_emoji_is_one_unit(self):
        self.assertEqual(units("<:koshian:123>"), ["<:koshian:123>"])
        self.assertIn("<a:cat:456>", units("あ<a:cat:456>い"))

    def test_combining_marks_stay_with_the_base(self):
        # 結合文字（e + ́）は1つの単位
        self.assertEqual(units("e\u0301"), ["e\u0301"])

    def test_patterns_are_compiled(self):
        self.assertEqual(CUSTOM.findall("<:koshian:123>"), [("koshian", "123")])
        self.assertIsNotNone(GRAPHEME.search("🍣"))


class LayoutTests(unittest.TestCase):
    """構図（33種類を等確率で抽選する）。"""

    def test_weights_cover_every_layout(self):
        names = [name for name, _ in LAYOUT_WEIGHTS]

        self.assertEqual(len(names), 33)
        self.assertEqual(names, list(LAYOUTS))
        self.assertEqual([weight for _, weight in LAYOUT_WEIGHTS], [1] * 33)
        self.assertEqual(tuple(names), AUTO_LAYOUTS)

    def test_every_layout_is_in_the_auto_pool(self):
        picked = {choose_layout(random.Random(value)) for value in range(800)}
        self.assertEqual(picked, set(AUTO_LAYOUTS))

    def test_mirror_alias_is_kept(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            outputs = [Path(tmp) / name for name in ("mirror.png", "mirrored.png")]
            for name, output in zip(("mirror", "mirrored"), outputs):
                render(avatar, "引用文", output, "名前", size="480x270",
                       layout=name, seed=1)
            with Image.open(outputs[0]) as a, Image.open(outputs[1]) as b:
                self.assertEqual(a.tobytes(), b.tobytes())

    def test_choose_layout_stays_in_range(self):
        for value in (0.0, 0.25, 0.5, 0.9, 0.999):
            self.assertIn(choose_layout(random.Random(value)), [n for n, _ in LAYOUT_WEIGHTS])

    def test_every_layout_renders_at_the_requested_size(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            for name, _ in LAYOUT_WEIGHTS:
                output = Path(tmp) / f"{name}.png"
                render(avatar, "こんにちは、世界。", output, "名前", "handle",
                       size="640x360", layout=name, seed=1)

                with Image.open(output) as image:
                    self.assertEqual(image.size, (640, 360))

    def test_unknown_layout_is_an_error(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            with self.assertRaises(ValueError):
                render(avatar, "テスト", Path(tmp) / "q.png", layout="unknown")

    def test_same_seed_gives_the_same_image(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            text = "同じ種なら同じ絵になるはずです。"

            render(avatar, text, first, "名前", "handle", seed=7)
            render(avatar, text, second, "名前", "handle", seed=7)

            with Image.open(first) as a, Image.open(second) as b:
                self.assertEqual(a.tobytes(), b.tobytes())

    # 上流 7949bca の回帰テスト。空白の無い長い1語（実在の長いURL）は
    # 本文が極小まで潰れ、mirror のアクセント縦罫の上端が下端を超えて
    # ValueError: y1 must be greater than or equal to y0 になっていた。
    LONG_WORD = "https://example.com/" + "a" * 120

    def test_mirror_keeps_a_long_unbreakable_word_inside_its_rule(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            for size in (None, "640x360", "200x120", "1920x1080"):
                output = Path(tmp) / "q.png"
                render(avatar, self.LONG_WORD, output, "名前", "handle",
                       size=size, layout="mirror")

                with Image.open(output) as image:
                    self.assertEqual(image.size, resolve_size(size))

    def test_mirror_long_word_survives_every_auto_seed(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            for seed in range(40):
                output = Path(tmp) / f"{seed}.png"
                render(avatar, self.LONG_WORD, output, "名前", "handle",
                       layout="auto", seed=seed)

                with Image.open(output) as image:
                    self.assertEqual(image.size, resolve_size(None))

    def test_mirror_rule_never_inverts_for_hard_text(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            for text in ("a" * 2000, "b" * 400, self.LONG_WORD, "x" * 90):
                for size in (None, "640x360", "200x120"):
                    output = Path(tmp) / "q.png"
                    render(avatar, text, output, size=size, layout="mirror")


class AsciiArtTests(unittest.TestCase):
    """アスキーアートの検出（AA用フォントが描けない文字はAAにしない）。"""

    def _detected(self, text):
        from jllm_plugins.miq import miq_aa

        return miq_aa.detect(text)

    def test_texts_the_aa_face_cannot_draw_are_not_ascii_art(self):
        # 記号比率だけでは "😀\\n😀" がAAと誤判定され、単一フォント
        # （Saitamaar）で描かれて絵文字が無言で消えていた。
        for text in ("😀\n😀", "🎉🎉\n🎉🎉", "مرحبا\nمرحبا", "𠮷野家\n𠮷野家",
                     "😀 すごい\n😀 すごい"):
            with self.subTest(text=text):
                self.assertIsNone(self._detected(text))

    def test_real_ascii_art_is_still_ascii_art(self):
        for name in ("mona", "tux", "dragon"):
            fixture = Path(__file__).parent / "aa" / f"{name}.txt"
            if not fixture.is_file():
                self.skipTest("AAのサンプル（tests/aa/*.txt）が無い環境")
            text = fixture.read_text(encoding="utf-8")
            with self.subTest(name=name):
                detected = self._detected(text)

                self.assertIsNotNone(detected)
                self.assertEqual(detected[0], "sjis" if name == "mona" else "mono")

    def test_emoji_only_text_renders_through_the_text_path(self):
        require_fonts(self)

        from jllm_plugins.miq.miq_fonts import family
        from jllm_plugins.miq.miq_layouts import Scene
        from jllm_plugins.miq.miq_text import EmojiImages

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            scene = Scene(Image.new("RGB", (64, 64)), "😀\n😀", "", "", (640, 360),
                          family("gothic"), EmojiImages(None))
            self.assertIsNone(scene.aa)

            output = Path(tmp) / "q.png"
            render(avatar, "😀\n😀", output, "名前", "@handle", layout="mirror")

            with Image.open(output) as image:
                self.assertEqual(image.size, resolve_size(None))
                # 絵文字の色（黄色）が実際に描かれている＝無言で消えていない
                rgb = image.convert("RGB").tobytes()

            self.assertTrue(any(
                rgb[i] > 200 and rgb[i + 1] > 150 and rgb[i + 2] < 120
                for i in range(0, len(rgb), 3)
            ))


class AlignTests(unittest.TestCase):
    """本文の寄せ方（MIQ_TEXT_ALIGN）。"""

    def test_choices_and_default(self):
        self.assertEqual(resolve_align(None), "right")     # 既定は右寄せ
        self.assertEqual(resolve_align("center"), "center")
        self.assertEqual(resolve_align("なにか"), "right")

    def test_center_takes_the_wider_area(self):
        center = text_region(640, 360, True, "center")
        right = text_region(640, 360, True, "right")

        self.assertLess(center[0], right[0])          # 中央寄せの方が左から使える

    def test_right_keeps_the_text_on_the_right_side(self):
        left, top, right, bottom = text_region(640, 360, True, "right")

        self.assertGreater((left + right) / 2, 640 * 0.6)

    def test_line_width_is_narrowed(self):
        left, top, right, bottom = text_region(640, 360, True, "right")

        # 1行が短くなるように右端を詰めてある（1920基準で1780）
        self.assertLessEqual(right, round(1780 * 640 / 1920) + 1)
        self.assertLess(right - left, 300)

    def test_render_honours_align(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            for align in ("center", "right"):
                output = Path(tmp) / f"{align}.png"
                render(avatar, "こんにちは", output, "名前", "handle",
                       None, None, "640x360", None, None, None, align,
                       layout="classic", seed=1)

                with Image.open(output) as image:
                    self.assertEqual(image.size, (640, 360))


class RenderTests(unittest.TestCase):
    def test_render_uses_the_default_size(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            output = Path(tmp) / "quote.png"

            render(avatar, "テストです。", output, "名前", "handle")

            with Image.open(output) as image:
                self.assertEqual(image.size, resolve_size(None))
                self.assertEqual(image.format, "PNG")

    def test_render_can_make_hd(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            output = Path(tmp) / "quote.png"
            render(avatar, "テストです。", output, size="1920x1080")

            with Image.open(output) as image:
                self.assertEqual(image.size, (1920, 1080))

    def test_long_text_renders_without_error(self):
        require_fonts(self)

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())
            output = Path(tmp) / "quote.png"

            render(avatar, "長い文章です。" * 300, output)

            with Image.open(output) as image:
                # 縦は伸ばさない（同じ比率のまま、潰して収める）
                self.assertEqual(image.size, resolve_size(None))

    def test_empty_text_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "avatar.png"
            avatar.write_bytes(avatar_png())

            with self.assertRaises(ValueError):
                render(avatar, "   ", Path(tmp) / "quote.png")


if __name__ == "__main__":
    unittest.main()
