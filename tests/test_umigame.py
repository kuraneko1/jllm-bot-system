# ruff: noqa: E501
"""Discord のウミガメのスープ（/umigame）のテスト。

Discord の通信はしない。投稿の解釈・開始宣言・⭕️一覧の整形と、
偽の Discord オブジェクトを使ったゲーム進行を確かめる。
"""
import asyncio
import unittest
from types import SimpleNamespace

import discord
from jllm_plugins.dashboard.turtle_soup import Puzzle, SoupGame
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.umigame.cog import (
    ChannelGame,
    Fact,
    UmigameCog,
    clip,
    custom_puzzle,
    format_facts,
    opening_text,
    parse_trigger,
    strip_skip_marks,
)


def puzzle():
    return Puzzle(
        id="test",
        title="テスト問題",
        problem="男は7階で降りて、階段で10階まで上がる。なぜ？",
        solution="ボタンに届かないから。",
        hints=("体に理由がある。",),
        difficulty=1,
        tags=("テスト",),
    )


class FakeD1:
    def __init__(self, answer="yes", content=0.9):
        self.answer = answer
        self.content = content

    async def close(self):
        return None

    async def ask(self, state, questions):
        if "answer" in questions:
            probabilities = {"yes": 0.9, "no": 0.05, "irrelevant": 0.05}

            if self.answer == "no":
                probabilities = {"yes": 0.05, "no": 0.9, "irrelevant": 0.05}

            return {"answer": {"type": "choice", "probabilities": probabilities}}

        answers = {"verdict": {"type": "choice",
                                "probabilities": {"ok": 0.8, "close": 0.15, "bad": 0.05}}}

        if "content" in questions:
            answers["content"] = {"type": "noul", "noul": self.content}

        return answers


class TriggerTests(unittest.TestCase):
    def test_question_with_double_question_marks(self):
        self.assertEqual(parse_trigger("男は海で遭難しましたか？？"),
                         ("question", "男は海で遭難しましたか"))
        self.assertEqual(parse_trigger("男は海で遭難しましたか??"),
                         ("question", "男は海で遭難しましたか"))

    def test_guess_with_double_exclamation_marks(self):
        self.assertEqual(parse_trigger("男は背が低いからだ！！"),
                         ("guess", "男は背が低いからだ"))

    def test_single_mark_is_ignored(self):
        self.assertIsNone(parse_trigger("男は海で遭難しましたか？"))
        self.assertIsNone(parse_trigger("男は背が低いからだ！"))

    def test_three_or_more_marks_still_work(self):
        self.assertEqual(parse_trigger("本当にそうなの？？？"),
                         ("question", "本当にそうなの"))
        self.assertEqual(parse_trigger("これだ！！！"), ("guess", "これだ"))

    def test_mixed_marks_are_ignored(self):
        self.assertIsNone(parse_trigger("どっち？！"))
        self.assertIsNone(parse_trigger("どっち！？"))

    def test_plain_text_and_marks_only_are_ignored(self):
        self.assertIsNone(parse_trigger("ただの雑談"))
        self.assertIsNone(parse_trigger("？？"))
        self.assertIsNone(parse_trigger("！！"))
        self.assertIsNone(parse_trigger(""))

    def test_skip_marks_are_stripped_before_parsing(self):
        self.assertEqual(parse_trigger(";男は海で遭難しましたか？？"),
                         ("question", "男は海で遭難しましたか"))
        self.assertEqual(parse_trigger(":男は海で遭難しましたか？？"),
                         ("question", "男は海で遭難しましたか"))
        self.assertEqual(parse_trigger("；男は海で遭難しましたか？？"),
                         ("question", "男は海で遭難しましたか"))
        self.assertEqual(parse_trigger("男は海で遭難しましたか？？;"),
                         ("question", "男は海で遭難しましたか"))

    def test_strip_skip_marks(self):
        self.assertEqual(strip_skip_marks(";: 本文 ："), "本文")
        self.assertEqual(strip_skip_marks(None), "")


class FormatTests(unittest.TestCase):
    def test_opening_has_problem_and_rules(self):
        text = opening_text(puzzle(), SimpleNamespace(mention="@user"))
        self.assertIn("> 男は7階で降りて、階段で10階まで上がる。なぜ？", text)
        self.assertIn("`??`", text)
        self.assertIn("`!!`", text)
        self.assertIn("@user", text)

    def test_facts_empty(self):
        self.assertIn("まだありません", format_facts([]))

    def test_facts_are_numbered_and_limited(self):
        facts = [Fact("A", "a"), Fact("B", "b"), Fact("C", "c")]
        text = format_facts(facts, limit=2)
        self.assertIn("1. A（a）", text)
        self.assertIn("2. B（b）", text)
        self.assertNotIn("3. C", text)
        self.assertIn("ほか 1 件", text)

    def test_clip(self):
        self.assertEqual(clip("abc", 5), "abc")
        self.assertEqual(clip("abcdef", 5), "abcd…")

    def test_custom_puzzle(self):
        custom = custom_puzzle("  問題  ", "  答え  ")
        self.assertEqual(custom.problem, "問題")
        self.assertEqual(custom.solution, "答え")
        self.assertTrue(custom.id.startswith("custom-"))


class UnlimitedGameTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_games_have_no_question_limit(self):
        game = SoupGame(id="g", puzzle=puzzle(), max_questions=None)
        client = FakeD1()

        for _ in range(3):
            await game.ask(client, "質問？")

        self.assertEqual(game.question_count, 3)
        self.assertIsNone(game.public()["max_questions"])

    async def test_channel_game_active_flag(self):
        game = SoupGame(id="g", puzzle=puzzle(), max_questions=None)
        state = ChannelGame(game=game, starter="tester")
        self.assertTrue(state.active)
        game.reveal()
        self.assertFalse(state.active)


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.modal = None

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modal = modal


class FakeUser:
    def __init__(self, name="tester", user_id=1, bot=False):
        self.id = user_id
        self.bot = bot
        self.display_name = name
        self.mention = f"<@{user_id}>"


class FakeChannel:
    def __init__(self, channel_id=20):
        self.id = channel_id


class FakeGuild:
    id = 10


class FakeInteraction:
    def __init__(self, channel_id=20):
        self.guild = FakeGuild()
        self.channel_id = channel_id
        self.user = FakeUser("starter", 2)
        self.response = FakeResponse()


class FakeMessage:
    def __init__(self, content, author=None, channel_id=20):
        self.content = content
        self.author = author or FakeUser()
        self.guild = FakeGuild()
        self.channel = FakeChannel(channel_id)
        self.reactions = []
        self.replies = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


class FakeBot:
    def __init__(self):
        self.restrictions = UserRestrictions(None)
        self.channel = FakeChannel()

    def get_channel(self, channel_id):
        return self.channel

    def is_ready(self):
        return True


class CogFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.cog = UmigameCog(self.bot)
        self.cog.client = FakeD1()  # d1 にはつながない
        self.cog.puzzles = {"test": puzzle()}  # ランダム選択を固定する
        self.interaction = FakeInteraction()
        await self.cog.start.callback(self.cog, self.interaction)

    async def asyncTearDown(self):
        await self.cog.cog_unload()
        await asyncio.sleep(0)

    async def test_start_posts_the_opening_declaration(self):
        content, kwargs = self.interaction.response.messages[0]
        self.assertIn("ウミガメのスープ", content)
        self.assertIn("`??`", content)
        self.assertIn("`/umigame end`", content)
        self.assertFalse(kwargs.get("ephemeral", False))
        self.assertTrue(self.cog.games[20].active)

    async def test_second_start_in_the_same_channel_is_refused(self):
        again = FakeInteraction()
        await self.cog.start.callback(self.cog, again)
        content, kwargs = again.response.messages[0]
        self.assertIn("ゲーム中", content)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_question_gets_a_circle_and_is_listed(self):
        message = FakeMessage(";男は背が低いですか？？")
        await self.cog.on_message(message)
        self.assertEqual(message.reactions, ["⭕"])
        self.assertEqual(len(self.cog.games[20].facts), 1)
        self.assertEqual(self.cog.games[20].facts[0].text, "男は背が低いですか")

    async def test_duplicate_yes_question_is_listed_once(self):
        await self.cog.on_message(FakeMessage("男は背が低いですか？？"))
        await self.cog.on_message(FakeMessage("男は背が低いですか??"))
        self.assertEqual(len(self.cog.games[20].facts), 1)

    async def test_no_answer_does_not_add_a_fact(self):
        self.cog.client = FakeD1(answer="no")
        message = FakeMessage("男は宇宙人ですか？？")
        await self.cog.on_message(message)
        self.assertEqual(message.reactions, ["❌"])
        self.assertEqual(self.cog.games[20].facts, [])

    async def test_plain_message_is_ignored(self):
        message = FakeMessage("ただの雑談")
        await self.cog.on_message(message)
        self.assertEqual(message.reactions, [])

    async def test_message_without_an_active_game_is_ignored(self):
        await self.cog.end.callback(self.cog, FakeInteraction())
        message = FakeMessage("男は背が低いですか？？")
        await self.cog.on_message(message)
        self.assertEqual(message.reactions, [])

    async def test_meta_guess_does_not_end_the_game(self):
        self.cog.client = FakeD1(content=0.1)
        message = FakeMessage("これが答え!: !!")
        await self.cog.on_message(message)
        self.assertEqual(self.cog.games[20].game.status, "playing")
        self.assertEqual(message.reactions, ["❌"])
        self.assertEqual(message.replies, [])

    async def test_correct_guess_replies_with_the_truth_and_ends(self):
        message = FakeMessage("ボタンに届かないからだ！！")
        await self.cog.on_message(message)
        self.assertEqual(self.cog.games[20].game.status, "solved")
        text, _kwargs = message.replies[0]
        self.assertIn("正解", text)
        self.assertIn("ボタンに届かないから。", text)

    async def test_end_command_reveals_and_allows_a_new_game(self):
        interaction = FakeInteraction()
        await self.cog.end.callback(self.cog, interaction)
        content, _kwargs = interaction.response.messages[0]
        self.assertIn("真相", content)
        self.assertIn("ボタンに届かないから。", content)
        self.assertFalse(self.cog.games[20].active)

        restarted = FakeInteraction()
        await self.cog.start.callback(self.cog, restarted)
        self.assertTrue(self.cog.games[20].active)

    async def test_facts_command_lists_yes_questions(self):
        await self.cog.on_message(FakeMessage("男は背が低いですか？？"))
        interaction = FakeInteraction()
        await self.cog.facts.callback(self.cog, interaction)
        content, _kwargs = interaction.response.messages[0]
        self.assertIn("確認できたこと", content)
        self.assertIn("男は背が低いですか", content)

    async def test_custom_modal_starts_a_custom_puzzle(self):
        interaction = FakeInteraction(channel_id=21)
        await self.cog.custom.callback(self.cog, interaction)
        modal = interaction.response.modal
        self.assertIsInstance(modal, discord.ui.Modal)
        self.assertEqual(modal.title, "ウミガメのスープ（自作）")

        modal.problem._value = " 自作の問題 "
        modal.solution._value = " 自作の答え "
        submit = FakeInteraction(channel_id=21)
        await modal.on_submit(submit)
        content, _kwargs = submit.response.messages[0]
        self.assertIn("> 自作の問題", content)
        self.assertNotIn("自作の答え", content)


if __name__ == "__main__":
    unittest.main()
