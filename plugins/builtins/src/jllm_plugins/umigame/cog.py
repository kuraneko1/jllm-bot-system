"""Discord のウミガメのスープ。d1-3B が出題者（GM）になる。

遊び方
- ``/umigame start``   … 同梱の問題からランダムに開始
- ``/umigame custom``  … モーダルで問題文と答えを指定（答えは本人にしか見えない）
- ``/umigame end``     … 強制終了して答えを出す
- ``/umigame facts``   … ⭕️ だった質問の一覧

チャンネルでの投稿
- 末尾 ``??`` … 質問。その投稿に ⭕️ / ❌️ / ❓️ を付ける
- 末尾 ``!!`` … 真相の推測。正解なら返信して終了、おしい 🤔、ちがう ❌️

先頭・末尾の ``;`` ``:``（全角も）は読み上げスキップの記号なので落としてから
判定する。読み上げ側（reader）は先頭の ``;``/``:`` で投稿を読まないので、
``;男は海で遭難しましたか？？`` で「読ませずに質問」ができる。
"""

# ruff: noqa: E501, E402
import asyncio
import logging
import random
import secrets
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands, tasks


@dataclass
class UmigameConfig:
    d1_api_url: str = "http://127.0.0.1:8083"
    d1_timeout_seconds: float = 20.0
    idle_minutes: int = 30


from jllm_plugins.dashboard.turtle_soup import (
    MAX_GUESS_CHARS,
    MAX_QUESTION_CHARS,
    D1Client,
    D1Error,
    Puzzle,
    SoupError,
    SoupGame,
    load_puzzles,
)
from jllm_plugins.restrictions.service import UserRestrictions

logger = logging.getLogger(__name__)

#: リアクション。⭕️/❌️/❓️ は質問への答え、🤔 は「おしい」推測、⚠️ は判定エラー。
EMOJI_YES = "⭕"
EMOJI_NO = "❌"
EMOJI_UNKNOWN = "❓"
EMOJI_CLOSE = "🤔"
EMOJI_ERROR = "⚠️"

#: 読み上げを止める記号（reader.SKIP_PREFIXES）。前処理で落とす。
SKIP_MARKS = ";:；："

#: Discord の1メッセージ上限（2000）に対する余裕
MAX_MESSAGE_CHARS = 1900

#: 一覧に出す最大件数
FACTS_LIMIT = 30

QUESTION_MARKS = "?？"
EXCLAIM_MARKS = "!！"


def strip_skip_marks(text) -> str:
    """先頭・末尾の読み上げスキップ記号と空白を落とす。"""

    return str(text or "").strip().strip(SKIP_MARKS + " \t\u3000")


def parse_trigger(content) -> tuple[str, str] | None:
    """投稿の末尾から質問（``??``）か推測（``!!``）を取り出す。

    末尾に2つ以上続く ``?``/``？`` を質問、``!``/``！`` を推測とみなす。
    記号を混ぜた ``?!`` や1つだけの ``?`` には反応しない。
    """

    text = strip_skip_marks(content)

    if not text:
        return None

    # 末尾の記号の並びを切り出す
    index = len(text)

    while index > 0 and text[index - 1] in QUESTION_MARKS + EXCLAIM_MARKS:
        index -= 1

    marks = text[index:]

    if len(marks) < 2:
        return None

    if any(char in EXCLAIM_MARKS for char in marks):
        if any(char in QUESTION_MARKS for char in marks):
            return None
        kind = "guess"
    else:
        kind = "question"

    body = text[:index].strip().strip(SKIP_MARKS).strip()

    if not body:
        return None

    return kind, body


def clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Discord の文字数上限に収める。"""

    if len(text) <= limit:
        return text

    return text[: limit - 1] + "…"


def opening_text(puzzle: Puzzle, author=None) -> str:
    """ゲーム開始時の宣言。"""

    problem = "\n".join("> " + line.strip() for line in puzzle.problem.splitlines() if line.strip())
    lines = [
        "🐢 **ウミガメのスープ**",
        problem,
        "",
        "質問は末尾に `??` を付けてください（はい／いいえで答えられる形で）。",
        "⭕️ はい　❌️ いいえ　❓️ 関係ない・判定できない",
        "真相を一文で書いて末尾に `!!`。正解なら答えを出して終了します。",
        "`/umigame end` 終了　`/umigame facts` ⭕️の一覧",
    ]

    if author is not None:
        lines.append(f"— {author.mention}")

    return clip("\n".join(lines))


def custom_puzzle(problem, solution) -> Puzzle:
    """モーダルで入力された問題文と答えから、その場限りの問題を作る。"""

    return Puzzle(
        id="custom-" + secrets.token_hex(4),
        title="自作の問題",
        problem=str(problem).strip(),
        solution=str(solution).strip(),
        hints=(),
        difficulty=1,
        tags=(),
    )


@dataclass
class Fact:
    """⭕️ だった質問（一覧に出す）。"""

    text: str
    author: str


@dataclass
class ChannelGame:
    """1チャンネル分のゲーム。終了後も facts を見られるよう残す。"""

    game: SoupGame
    starter: str
    facts: list[Fact] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def active(self) -> bool:
        return self.game.status == "playing"


def format_facts(facts, limit: int = FACTS_LIMIT) -> str:
    """⭕️ の一覧を整形する。"""

    if not facts:
        return "⭕️ 確認できたことは、まだありません。"

    lines = [f"⭕️ **確認できたこと（{len(facts)}件）**"]

    for index, fact in enumerate(facts[:limit], 1):
        lines.append(f"{index}. {fact.text}（{fact.author}）")

    if len(facts) > limit:
        lines.append(f"…ほか {len(facts) - limit} 件")

    return clip("\n".join(lines))


def result_text(state: ChannelGame, header: str) -> str:
    """終了時の文面（真相＋件数＋確認できたこと）。"""

    game = state.game
    text = (
        f"{header}\n\n**真相**\n{game.puzzle.solution}\n\n"
        f"（質問 {game.question_count}回 ・ ⭕️ {len(state.facts)}件）"
    )

    if state.facts:
        text += "\n\n" + format_facts(state.facts, limit=20)

    return clip(text)


class CustomModal(discord.ui.Modal, title="ウミガメのスープ（自作）"):
    problem = discord.ui.TextInput(
        label="問題文",
        style=discord.TextStyle.paragraph,
        placeholder="一見不可解な状況を書く（例：男は7階で降りて階段で10階へ上がる。なぜ？）",
        max_length=800,
        required=True,
    )
    solution = discord.ui.TextInput(
        label="答え（真相）",
        style=discord.TextStyle.paragraph,
        placeholder="質問に答えられる粒度で1〜2文",
        max_length=1500,
        required=True,
    )

    def __init__(self, cog: UmigameCog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.begin_custom(interaction, self.problem.value, self.solution.value)


class UmigameCog(commands.Cog):
    umigame = app_commands.Group(
        name="umigame",
        description="ウミガメのスープ（水平思考パズル）。d1 が出題者になります。",
    )

    def __init__(self, bot, config=None, restrictions=None, client=None):
        self.bot = bot
        self.config = config or UmigameConfig()
        self.restrictions = restrictions or getattr(bot, "restrictions", None) or UserRestrictions()
        self.client = client or D1Client(self.config.d1_api_url, self.config.d1_timeout_seconds)
        self.puzzles = load_puzzles()
        self.games: dict[int, ChannelGame] = {}
        self.sweeper.start()

    async def cog_unload(self):
        self.sweeper.cancel()
        await self.client.close()

    # ------------------------------------------------------------ 開始・終了

    def _active(self, channel_id) -> ChannelGame | None:
        state = self.games.get(int(channel_id))

        if state is None or not state.active:
            return None

        return state

    def _random_puzzle(self, channel_id) -> Puzzle:
        choices = list(self.puzzles.values())
        previous = self.games.get(int(channel_id))

        if previous is not None and len(choices) > 1:
            choices = [p for p in choices if p.id != previous.game.puzzle.id] or choices

        return random.choice(choices)

    async def _begin(self, interaction: discord.Interaction, puzzle: Puzzle):
        if interaction.guild is None:
            await interaction.response.send_message(
                "サーバーのチャンネルで使ってください。", ephemeral=True
            )
            return

        running = self._active(interaction.channel_id)

        if running is not None:
            await interaction.response.send_message(
                "このチャンネルではゲーム中です。"
                f"いま {running.game.question_count} 問目まで進んでいます。"
                "終了するには `/umigame end`。",
                ephemeral=True,
            )
            return

        game = SoupGame(
            id=secrets.token_urlsafe(9),
            puzzle=puzzle,
            max_questions=None,  # Discord では質問数を制限しない
        )
        self.games[int(interaction.channel_id)] = ChannelGame(
            game=game,
            starter=interaction.user.display_name,
        )
        await interaction.response.send_message(
            opening_text(puzzle, interaction.user),
            allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
        )

    async def begin_custom(self, interaction: discord.Interaction, problem, answer):
        if not str(problem or "").strip() or not str(answer or "").strip():
            await interaction.response.send_message(
                "問題文と答えの両方を入力してください。", ephemeral=True
            )
            return

        await self._begin(interaction, custom_puzzle(problem, answer))

    @umigame.command(name="start", description="同梱の問題からランダムに始める")
    async def start(self, interaction: discord.Interaction):
        await self._begin(interaction, self._random_puzzle(interaction.channel_id))

    @umigame.command(name="custom", description="問題文と答えを指定して始める")
    async def custom(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "サーバーのチャンネルで使ってください。", ephemeral=True
            )
            return

        if self._active(interaction.channel_id) is not None:
            await interaction.response.send_message(
                "このチャンネルではゲーム中です。終了するには `/umigame end`。",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(CustomModal(self))

    @umigame.command(name="end", description="いまのゲームを終了して答えを出す")
    async def end(self, interaction: discord.Interaction):
        state = self._active(interaction.channel_id)

        if state is None:
            await interaction.response.send_message(
                "このチャンネルではゲーム中ではありません。", ephemeral=True
            )
            return

        state.game.reveal()
        state.last_active = time.monotonic()
        await interaction.response.send_message(
            result_text(state, "🏁 終了しました。"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @umigame.command(name="facts", description="⭕️だった質問の一覧")
    async def facts(self, interaction: discord.Interaction):
        state = self.games.get(int(interaction.channel_id))

        if state is None:
            await interaction.response.send_message(
                "このチャンネルではゲームをしていません。", ephemeral=True
            )
            return

        await interaction.response.send_message(
            format_facts(state.facts),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------------ 投稿の処理

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        state = self._active(message.channel.id)

        if state is None:
            return

        parsed = parse_trigger(message.content)

        if parsed is None:
            return

        try:
            if self.restrictions.ignored(message.guild.id, message.author.id):
                return
        except Exception:  # noqa: BLE001 制限の読み込み失敗でゲームを止めない
            logger.warning("ユーザー制限を確認できませんでした", exc_info=True)

        kind, body = parsed

        if len(body) > (MAX_QUESTION_CHARS if kind == "question" else MAX_GUESS_CHARS):
            await self._react(message, EMOJI_ERROR)
            return

        async with state.lock:
            if not state.active:
                return

            try:
                if kind == "question":
                    turn = await state.game.ask(self.client, body)
                else:
                    turn = await state.game.guess(self.client, body)
            except SoupError:
                return
            except D1Error:
                logger.warning("ウミガメの判定に失敗しました", exc_info=True)
                await self._react(message, EMOJI_ERROR)
                return

        state.last_active = time.monotonic()

        if kind == "question":
            await self._react(
                message,
                {"yes": EMOJI_YES, "no": EMOJI_NO}.get(turn["answer"], EMOJI_UNKNOWN),
            )
            if turn["answer"] == "yes":
                self._add_fact(state, body, message.author.display_name)
            return

        verdict = turn["verdict"]

        if verdict == "correct":
            await self._reply(
                message,
                result_text(state, f"🎉 正解！ {message.author.mention}"),
                allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )
        elif verdict == "close":
            await self._react(message, EMOJI_CLOSE)
        else:
            await self._react(message, EMOJI_NO)

    @staticmethod
    def _add_fact(state: ChannelGame, text: str, author: str):
        key = " ".join(str(text).split())

        if any(" ".join(fact.text.split()) == key for fact in state.facts):
            return

        state.facts.append(Fact(text=str(text), author=str(author)))

    @staticmethod
    async def _react(message: discord.Message, emoji: str):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            logger.warning("リアクションに失敗しました: %s", emoji, exc_info=True)

    @staticmethod
    async def _reply(message: discord.Message, text: str, allowed_mentions=None):
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=allowed_mentions or discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning("返信に失敗しました", exc_info=True)

    # ------------------------------------------------------------ 後片付け

    @tasks.loop(minutes=5)
    async def sweeper(self):
        """操作が無いまま放置されたゲームを終了する。"""

        if not self.bot.is_ready():
            return

        now = time.monotonic()
        timeout = self.config.idle_minutes * 60

        for channel_id, state in list(self.games.items()):
            if not state.active or now - state.last_active < timeout:
                continue

            state.game.reveal()
            channel = self.bot.get_channel(channel_id)

            if channel is None:
                continue

            try:
                await channel.send(
                    result_text(
                        state,
                        f"⌛ {self.config.idle_minutes}分操作がなかったので終了しました。",
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.warning("自動終了の通知に失敗しました", exc_info=True)
