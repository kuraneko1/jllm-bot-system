# ruff: noqa: E501
"""ウミガメのスープ（水平思考パズル）の自動出題。

d1-3B の System One API を**出題者（GM）**として使う。d1 は文章を生成せず、
型付きの確率だけを返す。そのため

- 質問への答え   : ``choice``（yes / no / irrelevant）の確率
- 推測の合否     : ``choice``（ok / close / bad）の確率

から決める。``confidence`` は最大確率ではないので使わない（D1-REFERENCE.md）。

ゲーム進行は HTTP に依存させない。Web UI も Discord も同じ ``TurtleSoup`` を
共有し、判定だけを ``D1Client`` に投げる。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from . import d1_lifecycle

logger = logging.getLogger(__name__)

PUZZLES_PATH = Path(__file__).with_name("turtle_soup_puzzles.json")

#: 1問の質問の上限（長すぎる質問は判定がぶれる）
MAX_QUESTION_CHARS = 200
#: 1回の推測の上限
MAX_GUESS_CHARS = 400
#: 1ゲームで聞ける質問の数
MAX_QUESTIONS = 100
#: これ未満の最大確率は「判定できません」にする（誤答を断定しない）
ANSWER_MIN_PROBABILITY = 0.40
#: 推測を「正解」と認める確率
GUESS_CORRECT_PROBABILITY = 0.50
#: 推測を「おしい」と認める確率（方向は合っている）
GUESS_CLOSE_PROBABILITY = 0.40
#: 同時に d1 へ投げる数（サーバーの ``-np`` に合わせる）
MAX_CONCURRENCY = 8

ANSWER_LABELS = {
    "yes": "はい",
    "no": "いいえ",
    "irrelevant": "関係ない",
    "unsure": "判定できません",
}
VERDICT_LABELS = {
    "correct": "正解",
    "close": "おしい",
    "wrong": "ちがう",
}

#: 質問への答え。ラベルはプロンプト内でも日本語で説明する。
ANSWER_CRITERIA = {
    "yes": "はい（質問は真相に照らして正しい）",
    "no": "いいえ（質問は真相と矛盾する）",
    "irrelevant": "関係ない（真相だけでは決められない・無関係）",
}
GUESS_CRITERIA = {
    "ok": "正解（真相の要点を言い当てている）",
    "close": "おしい（方向は合っているが要点が欠けている）",
    "bad": "ちがう（見当違い）",
}

QUESTION_INSTRUCTIONS = (
    "あなたはウミガメのスープの出題者です。下の「問題」と「真相」を知っています。"
    "参加者の質問が、真相に照らして正しいか答えてください。"
    "真相に書かれていないことは「関係ない」です。"
    "問題文や真相に書かれていない設定（夢の中、想像の話、超常現象など）を補って判定しないでください。"
    "質問："
)

GUESS_INSTRUCTIONS = (
    "参加者の推測が、真相の要点（なぜそうなったのか）を言い当てているか判定してください。"
    "細部が足りなくても、要点が合っていれば「正解」です。"
    "「〜ですか？」のような疑問文の形で書かれていても、内容が要点を言い当てていれば「正解」とみなします。"
    "推測："
)

#: 推測が「真相の内容」を含むかを別に聞く。d1 は「これが答え！」のような
#: 中身のない宣言を「正解」と読むことがあるので、正解のときだけゲートする。
CONTENT_INSTRUCTIONS = (
    "この文章は、ウミガメのスープの真相に含まれる具体的な人物・物・場所・理由のいずれかを名指ししていますか？"
    "「これが答え」「正解はこれ」「当たり」のような、内容を書いていない宣言は「いいえ」です。"
    "文章："
)
#: これを下回る推測は、d1 が「正解」と言っても終了しない（内容がないため）
CONTENT_MIN_PROBABILITY = 0.20

#: 正解にする推測の最短文字数。単語ひとつ（「医者」「氷」）では終了せず、
#: 「なぜそうなったか」を一文で書いてもらう。
MIN_GUESS_CHARS = 10

#: 打ち消しの「推測」が真相に触れているかを見るための文字数の単位。
#: 真相と共通する3文字が1つもなければ、d1 が正解と言っても「おしい」にする。
#: 「海が関係しています」のような話題だけの推測を弾くための最後の砦。
FRAGMENT_SIZE = 3
_FRAGMENT_STRIP = re.compile(
    r"[\s、。，．・「」『』（）()\[\]【】!?！？,.;:：；…\-―—~〜\"'“”‘’]+"
)
#: どの真相にも出てきやすい文法だけの断片（これだけの一致では根拠にしない）
_STOP_FRAGMENTS = {
    "ので", "のに", "のは", "のが", "には", "では", "とは", "でも", "から", "まで",
    "より", "ほど", "して", "てい", "てる", "いる", "いた", "した", "する", "され",
    "れる", "られ", "だっ", "った", "って", "ます", "です", "こと", "もの", "よう",
    "そう", "なの", "この", "その", "あの", "どの", "ため", "ところ", "とき", "また",
    "まだ", "もう",
}


def shared_fragments(guess, solution, size: int = FRAGMENT_SIZE) -> int:
    """推測と真相に共通する文字の断片（3文字）の数。"""

    left = _FRAGMENT_STRIP.sub("", str(guess))
    right = _FRAGMENT_STRIP.sub("", str(solution))

    if len(left) < size:
        return 0

    grams = {left[index:index + size] for index in range(len(left) - size + 1)}

    return sum(
        1 for gram in grams
        if gram not in _STOP_FRAGMENTS and gram in right
    )


class D1Error(Exception):
    """d1 に聞けなかった。呼び出し側は利用者向けのエラーにする。"""


class SoupError(Exception):
    """利用者にそのまま見せてよいエラー（入力の不備など）。"""


@dataclass(frozen=True)
class Puzzle:
    id: str
    title: str
    problem: str
    solution: str
    hints: tuple[str, ...]
    difficulty: int
    tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> Puzzle:
        hints = data.get("hints") or ()
        tags = data.get("tags") or ()

        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or data["id"]),
            problem=str(data["problem"]),
            solution=str(data["solution"]),
            hints=tuple(str(hint) for hint in hints if str(hint).strip()),
            difficulty=max(1, min(3, int(data.get("difficulty", 1)))),
            tags=tuple(str(tag) for tag in tags if str(tag).strip()),
        )

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "problem": self.problem,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "hint_total": len(self.hints),
        }

    def state(self) -> dict:
        """d1 に渡す状態。問題文と真相だけを渡す（ヒントは渡さない）。"""

        return {"問題": self.problem, "真相": self.solution}


def load_puzzles(path: Path | str | None = None) -> dict[str, Puzzle]:
    """同梱の JSON から問題を読む（壊れていたら例外）。"""

    source = Path(path) if path is not None else PUZZLES_PATH
    data = json.loads(source.read_text(encoding="utf-8"))
    puzzles: dict[str, Puzzle] = {}

    for item in data.get("puzzles") or ():
        puzzle = Puzzle.from_dict(item)

        if puzzle.id in puzzles:
            raise ValueError(f"問題IDが重複しています: {puzzle.id}")

        puzzles[puzzle.id] = puzzle

    if not puzzles:
        raise ValueError("ウミガメのスープの問題が1問もありません。")

    return puzzles


def normalize_text(text: Any) -> str:
    """入力を1行に均す（改行と連続空白を詰める）。"""

    return " ".join(str(text or "").split())


def _probabilities(answer: dict, keys) -> dict[str, float]:
    raw = answer.get("probabilities") or {}
    values: dict[str, float] = {}

    for key in keys:
        try:
            values[key] = round(max(0.0, min(1.0, float(raw.get(key, 0.0)))), 4)
        except (TypeError, ValueError):
            values[key] = 0.0

    return values


def judge_answer(answer: dict) -> dict:
    """d1 の choice を「はい／いいえ／関係ない／判定できません」に落とす。"""

    probabilities = _probabilities(answer, ANSWER_CRITERIA)
    key = max(probabilities, key=probabilities.__getitem__)
    probability = probabilities[key]

    if probability < ANSWER_MIN_PROBABILITY:
        key = "unsure"

    return {
        "answer": key,
        "label": ANSWER_LABELS[key],
        "probabilities": probabilities,
        "probability": probability,
    }


def judge_guess(
    answer: dict,
    content_probability: float = 1.0,
    fragments: int = 1,
    chars: int = MIN_GUESS_CHARS,
) -> dict:
    """d1 の choice を「正解／おしい／ちがう」に落とす。

    正解にしないためのゲートが2つある。

    - ``content_probability``: 「推測が真相の内容を含むか」の確率。
      0.20 未満（「これが答え！」など）は「ちがう」。
    - ``fragments``: 推測と真相に共通する3文字の断片の数。
      0（「海が関係しています」のような話題だけ）は「おしい」。
    - ``chars``: 推測の文字数。10文字未満（単語ひとつ）は「おしい」。
      「医者」「氷」のような正しい一言も、理由を書いてもらう。
    """

    probabilities = _probabilities(answer, GUESS_CRITERIA)
    ok = probabilities["ok"]
    close = probabilities["close"]

    if ok >= GUESS_CORRECT_PROBABILITY and ok >= close:
        verdict = "correct"
    elif close >= GUESS_CLOSE_PROBABILITY or ok >= GUESS_CORRECT_PROBABILITY - 0.2:
        verdict = "close"
    else:
        verdict = "wrong"

    try:
        content = max(0.0, min(1.0, float(content_probability)))
    except (TypeError, ValueError):
        content = 1.0

    try:
        fragments = max(0, int(fragments))
    except (TypeError, ValueError):
        fragments = 1

    try:
        chars = max(0, int(chars))
    except (TypeError, ValueError):
        chars = MIN_GUESS_CHARS

    if verdict == "correct":
        if content < CONTENT_MIN_PROBABILITY:
            verdict = "wrong"
        elif fragments < 1 or chars < MIN_GUESS_CHARS:
            verdict = "close"

    return {
        "verdict": verdict,
        "label": VERDICT_LABELS[verdict],
        "probabilities": probabilities,
        "probability": probabilities[{"correct": "ok", "close": "close", "wrong": "bad"}[verdict]],
        "content": round(content, 4),
        "fragments": fragments,
        "chars": chars,
    }


class D1Client:
    """llama.cpp の ``/v1/systemone`` を叩く最小クライアント。"""

    def __init__(self, url: str, timeout: float = 20.0, concurrency: int = MAX_CONCURRENCY):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )

        return self._session

    async def ask(self, state: Any, questions: dict) -> dict:
        """1回の順伝播で複数の質問をまとめて聞く。"""

        if not questions:
            raise D1Error("質問がありません。")

        # d1 は遊んでいないあいだ止めてある（VRAM 約4GiB）。設定された d1 を叩くときだけ、
        # 必要なら起こす（テストや別の d1 を指すクライアントには触らない）。
        lifecycle = d1_lifecycle.get()

        if lifecycle.manages(self.url):
            try:
                await lifecycle.ensure()
            except Exception:  # noqa: BLE001 起こせなくてもリクエストは試す
                logger.warning("d1 の起動確認に失敗しました", exc_info=True)

        session = await self._ensure_session()
        body = {"state": state, "questions": questions}

        async with self._semaphore:
            try:
                async with session.post(
                    self.url + "/v1/systemone",
                    json=body,
                ) as response:
                    payload = await response.json(content_type=None)

                    if response.status != 200:
                        message = ""
                        if isinstance(payload, dict):
                            message = str((payload.get("error") or {}).get("message") or "")
                        raise D1Error(
                            f"判定エンジンがエラーを返しました（{response.status}）。{message}".strip()
                        )
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise D1Error("判定エンジンに接続できませんでした。") from exc
            except ValueError as exc:
                raise D1Error("判定エンジンの応答を読めませんでした。") from exc

        answers = payload.get("answers") if isinstance(payload, dict) else None

        if not isinstance(answers, dict):
            raise D1Error("判定エンジンの応答が不正です。")

        return answers

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


@dataclass
class SoupGame:
    """1回分のゲーム。問題・質問履歴・状態を持つ。"""

    id: str
    puzzle: Puzzle
    turns: list = field(default_factory=list)
    hints_shown: list = field(default_factory=list)
    solved: bool = False
    revealed: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    max_questions: int | None = MAX_QUESTIONS

    @property
    def status(self) -> str:
        if self.solved:
            return "solved"
        if self.revealed:
            return "revealed"
        return "playing"

    @property
    def question_count(self) -> int:
        return sum(1 for turn in self.turns if turn.get("kind") == "question")

    def _ensure_playing(self) -> None:
        if self.status != "playing":
            raise SoupError("この問題は終了しています。新しく始めてください。")

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "puzzle": self.puzzle.public(),
            "turns": list(self.turns),
            "question_count": self.question_count,
            "hint_total": len(self.puzzle.hints),
            "hints_shown": list(self.hints_shown),
            "max_questions": self.max_questions,
            "solution": self.puzzle.solution if self.status != "playing" else None,
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at) if self.finished_at else None,
        }

    async def ask(self, client: D1Client, question: Any) -> dict:
        """質問を1つ聞く。答えは turn として履歴に残す。"""

        self._ensure_playing()
        text = normalize_text(question)

        if not text:
            raise SoupError("質問を入力してください。")
        if len(text) > MAX_QUESTION_CHARS:
            raise SoupError(f"質問は{MAX_QUESTION_CHARS}文字以内にしてください。")
        if self.max_questions is not None and self.question_count >= self.max_questions:
            raise SoupError(f"質問が上限（{self.max_questions}回）に達しました。答えを見てください。")

        answers = await client.ask(
            self.puzzle.state(),
            {
                "answer": {
                    "type": "choice",
                    "instructions": QUESTION_INSTRUCTIONS + text,
                    "criteria": ANSWER_CRITERIA,
                }
            },
        )
        judged = judge_answer(answers.get("answer") or {})
        turn = {
            "kind": "question",
            "text": text,
            "answer": judged["answer"],
            "label": judged["label"],
            "probabilities": judged["probabilities"],
            "probability": judged["probability"],
            "at": int(time.time()),
        }
        self.turns.append(turn)
        return turn

    async def guess(self, client: D1Client, guess: Any) -> dict:
        """推測を判定する。核心なら ``solved`` にする。"""

        self._ensure_playing()
        text = normalize_text(guess)

        if not text:
            raise SoupError("推測を入力してください。")
        if len(text) > MAX_GUESS_CHARS:
            raise SoupError(f"推測は{MAX_GUESS_CHARS}文字以内にしてください。")

        answers = await client.ask(
            self.puzzle.state(),
            {
                "verdict": {
                    "type": "choice",
                    "instructions": GUESS_INSTRUCTIONS + text,
                    "criteria": GUESS_CRITERIA,
                },
                "content": {
                    "type": "noul",
                    "instructions": CONTENT_INSTRUCTIONS + text,
                },
            },
        )
        content = answers.get("content") or {}
        fragments = shared_fragments(text, self.puzzle.solution)
        judged = judge_guess(
            answers.get("verdict") or {},
            content.get("noul", 1.0),
            fragments,
            len(text),
        )
        turn = {
            "kind": "guess",
            "text": text,
            "verdict": judged["verdict"],
            "label": judged["label"],
            "probabilities": judged["probabilities"],
            "probability": judged["probability"],
            "content": judged["content"],
            "fragments": judged["fragments"],
            "chars": judged["chars"],
            "at": int(time.time()),
        }
        self.turns.append(turn)

        if judged["verdict"] == "correct":
            self.solved = True
            self.finished_at = time.time()

        return turn

    def hint(self) -> dict:
        """次のヒントを1つ出す（出し切ったら最後のヒントを返す）。"""

        self._ensure_playing()

        if not self.puzzle.hints:
            raise SoupError("この問題にはヒントがありません。")

        index = min(len(self.hints_shown), len(self.puzzle.hints) - 1)
        text = self.puzzle.hints[index]
        exhausted = len(self.hints_shown) >= len(self.puzzle.hints)

        if not exhausted:
            self.hints_shown.append(text)

        turn = {
            "kind": "hint",
            "text": text,
            "label": "ヒント",
            "at": int(time.time()),
        }
        self.turns.append(turn)
        return turn

    def reveal(self) -> dict:
        """答えを見せて終了する（解けなかった扱い）。"""

        self._ensure_playing()
        self.revealed = True
        self.finished_at = time.time()

        return {
            "kind": "reveal",
            "text": self.puzzle.solution,
            "label": "答え",
            "at": int(time.time()),
        }


class TurtleSoup:
    """問題集とゲームの保管。Web UI と Discord の両方から使う。"""

    def __init__(
        self,
        client: D1Client,
        puzzles: dict[str, Puzzle] | None = None,
        *,
        ttl_seconds: float = 3 * 60 * 60,
        max_games: int = 300,
        max_questions: int | None = MAX_QUESTIONS,
        random_source: random.Random | None = None,
    ):
        self.client = client
        self.puzzles = puzzles if puzzles is not None else load_puzzles()
        self.ttl_seconds = ttl_seconds
        self.max_games = max(1, int(max_games))
        self.max_questions = (
            None if max_questions is None else max(1, int(max_questions))
        )
        self.games: dict[str, SoupGame] = {}
        self._random = random_source or random.Random()
        # bot を再起動したときに d1 が立ちっぱなしなら、ここから見張って解放する
        d1_lifecycle.get().touch()

    # ---------------------------------------------------------------- 問題

    def puzzle_list(self) -> list[dict]:
        return [
            puzzle.public()
            for puzzle in sorted(
                self.puzzles.values(),
                key=lambda item: (item.difficulty, item.id),
            )
        ]

    def new_game(self, puzzle_id: str | None = None) -> SoupGame:
        """新しいゲームを始める（ID 未指定なら難易度1からランダム）。"""

        self._prune()

        if puzzle_id:
            puzzle = self.puzzles.get(str(puzzle_id))

            if puzzle is None:
                raise SoupError("その問題は見つかりません。")
        else:
            easy = [p for p in self.puzzles.values() if p.difficulty <= 2]
            puzzle = self._random.choice(easy or list(self.puzzles.values()))

        game = SoupGame(id=secrets.token_urlsafe(9), puzzle=puzzle,
                        max_questions=self.max_questions)
        self.games[game.id] = game
        return game

    def get(self, game_id: Any) -> SoupGame | None:
        if not game_id:
            return None

        return self.games.get(str(game_id))

    def require(self, game_id: Any) -> SoupGame:
        game = self.get(game_id)

        if game is None:
            raise SoupError("ゲームが見つかりません。新しく始めてください。")

        return game

    def _prune(self) -> None:
        now = time.time()
        expired = [
            key
            for key, game in self.games.items()
            if now - (game.finished_at or game.started_at) > self.ttl_seconds
        ]

        for key in expired:
            self.games.pop(key, None)

        if len(self.games) >= self.max_games:
            # 古い順に捨てる（始めた時刻）
            for key, _game in sorted(self.games.items(), key=lambda item: item[1].started_at)[
                : len(self.games) - self.max_games + 1
            ]:
                self.games.pop(key, None)

    async def close(self) -> None:
        await self.client.close()
