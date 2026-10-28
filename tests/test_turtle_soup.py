# ruff: noqa: E501
"""ウミガメのスープ（d1 を出題者にする水平思考パズル）のテスト。

d1 本体にはつながない。``D1Client`` と同じ ``ask(state, questions)`` を持つ
偽クライアントを渡し、判定のしきい値・ゲーム進行・Web API を確かめる。
"""
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import ReadingDashboard
from jllm_plugins.dashboard.turtle_soup import (
    ANSWER_MIN_PROBABILITY,
    MAX_QUESTION_CHARS,
    D1Client,
    Puzzle,
    SoupError,
    TurtleSoup,
    judge_answer,
    judge_guess,
    load_puzzles,
    normalize_text,
    shared_fragments,
)
from jllm_plugins.dictionaries.readings import ReadingDictionary


class FakeD1:
    """d1 の代わり。質問の種類ごとに決まった確率を返す。"""

    def __init__(self, answer="yes", verdict="ok", content=0.9):
        self.answer = answer
        self.verdict = verdict
        self.content = content
        self.calls = []
        self.closed = False

    async def ask(self, state, questions):
        self.calls.append((state, questions))

        if "answer" in questions:
            probabilities = {"yes": 0.9, "no": 0.05, "irrelevant": 0.05}

            if self.answer != "yes":
                probabilities = {"yes": 0.05, "no": 0.9, "irrelevant": 0.05}

            return {"answer": {"type": "choice", "choice": self.answer,
                               "probabilities": probabilities}}

        probabilities = {"ok": 0.7, "close": 0.2, "bad": 0.1}

        if self.verdict != "ok":
            probabilities = {"ok": 0.05, "close": 0.15, "bad": 0.8}

        answers = {"verdict": {"type": "choice", "choice": self.verdict,
                               "probabilities": probabilities}}

        if "content" in questions:
            answers["content"] = {"type": "noul", "noul": self.content}

        return answers

    async def close(self):
        self.closed = True


def make_puzzle(**overrides):
    data = {
        "id": "test-puzzle",
        "title": "テスト問題",
        "difficulty": 1,
        "tags": ["テスト"],
        "problem": "男が7階で降りた。なぜ？",
        "solution": "ボタンに届かなかったから。",
        "hints": ["体に理由がある。", "雨の日は違う。"],
    }
    data.update(overrides)
    return Puzzle.from_dict(data)


class JudgeTests(unittest.TestCase):
    def test_top_probability_decides_the_answer(self):
        judged = judge_answer({"probabilities": {"yes": 0.2, "no": 0.7, "irrelevant": 0.1}})
        self.assertEqual(judged["answer"], "no")
        self.assertEqual(judged["label"], "いいえ")

    def test_low_probability_becomes_unsure(self):
        judged = judge_answer({"probabilities": {"yes": 0.35, "no": 0.34, "irrelevant": 0.31}})
        self.assertEqual(judged["answer"], "unsure")
        self.assertLess(judged["probability"], ANSWER_MIN_PROBABILITY)

    def test_confidence_is_not_used(self):
        # confidence（0.4）が最大確率（0.6）より小さくても、最大確率で判定する。
        judged = judge_answer({"probabilities": {"yes": 0.6, "no": 0.3, "irrelevant": 0.1},
                               "confidence": 0.4})
        self.assertEqual(judged["label"], "はい")

    def test_content_free_guess_is_never_correct(self):
        judged = judge_guess({"probabilities": {"ok": 0.85, "close": 0.06, "bad": 0.09}},
                             content_probability=0.15)
        self.assertEqual(judged["verdict"], "wrong")
        self.assertEqual(judged["content"], 0.15)

    def test_ungrounded_guess_is_close_not_correct(self):
        judged = judge_guess({"probabilities": {"ok": 0.8, "close": 0.1, "bad": 0.1}},
                             content_probability=0.8, fragments=0)
        self.assertEqual(judged["verdict"], "close")

    def test_grounded_guess_can_be_correct(self):
        judged = judge_guess({"probabilities": {"ok": 0.8, "close": 0.1, "bad": 0.1}},
                             content_probability=0.8, fragments=2, chars=12)
        self.assertEqual(judged["verdict"], "correct")

    def test_single_word_guess_is_close(self):
        judged = judge_guess({"probabilities": {"ok": 0.8, "close": 0.1, "bad": 0.1}},
                             content_probability=0.8, fragments=2, chars=2)
        self.assertEqual(judged["verdict"], "close")

    def test_shared_fragments(self):
        self.assertGreater(shared_fragments("ボタンに届かないから", "ボタンに届かなかったから。"), 0)
        self.assertEqual(shared_fragments("海が関係しています", "ボタンに届かなかったから。"), 0)
        self.assertEqual(shared_fragments("な", "ながい"), 0)

    def test_content_gate_does_not_touch_close_or_wrong(self):
        close = judge_guess({"probabilities": {"ok": 0.3, "close": 0.5, "bad": 0.2}},
                            content_probability=0.05)
        self.assertEqual(close["verdict"], "close")

    def test_guess_verdicts(self):
        self.assertEqual(judge_guess({"probabilities": {"ok": 0.6, "close": 0.3, "bad": 0.1}})["verdict"],
                         "correct")
        self.assertEqual(judge_guess({"probabilities": {"ok": 0.3, "close": 0.5, "bad": 0.2}})["verdict"],
                         "close")
        judged = judge_guess({"probabilities": {"ok": 0.1, "close": 0.2, "bad": 0.7}})
        self.assertEqual(judged["verdict"], "wrong")
        # 表示する確率は、その判定の確率（"bad"）を返す。
        self.assertEqual(judged["probability"], 0.7)

    def test_normalize_text(self):
        self.assertEqual(normalize_text("  男は\n  海で  "), "男は 海で")


class GameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeD1()
        self.soup = TurtleSoup(self.client, puzzles={"test-puzzle": make_puzzle()})
        self.game = self.soup.new_game("test-puzzle")

    async def test_ask_records_a_turn_and_sends_problem_and_solution(self):
        turn = await self.game.ask(self.client, "ボタンに届きますか？")
        self.assertEqual(turn["kind"], "question")
        self.assertEqual(turn["label"], "はい")
        self.assertEqual(self.game.question_count, 1)
        state = self.client.calls[0][0]
        self.assertEqual(state["問題"], "男が7階で降りた。なぜ？")
        self.assertEqual(state["真相"], "ボタンに届かなかったから。")

    async def test_meta_guess_does_not_finish_the_game(self):
        client = FakeD1(content=0.1)
        turn = await self.game.guess(client, "これが答え!")
        self.assertEqual(turn["verdict"], "wrong")
        self.assertEqual(self.game.status, "playing")
        self.assertIsNone(self.game.public()["solution"])

    async def test_guess_correct_finishes_and_reveals_solution(self):
        turn = await self.game.guess(self.client, "ボタンに届かないから")
        self.assertEqual(turn["verdict"], "correct")
        self.assertEqual(self.game.status, "solved")
        self.assertEqual(self.game.public()["solution"], "ボタンに届かなかったから。")

    async def test_guess_wrong_does_not_finish(self):
        client = FakeD1(verdict="bad")
        turn = await self.game.guess(client, "宇宙人だから")
        self.assertEqual(turn["verdict"], "wrong")
        self.assertEqual(self.game.status, "playing")
        self.assertIsNone(self.game.public()["solution"])

    async def test_hint_progresses_and_repeats_the_last_one(self):
        self.assertEqual(self.game.hint()["text"], "体に理由がある。")
        self.assertEqual(self.game.hint()["text"], "雨の日は違う。")
        self.assertEqual(self.game.hint()["text"], "雨の日は違う。")
        self.assertEqual(len(self.game.hints_shown), 2)

    async def test_reveal_finishes(self):
        self.game.reveal()
        self.assertEqual(self.game.status, "revealed")
        self.assertIsNotNone(self.game.public()["solution"])

    async def test_finished_game_refuses_more_questions(self):
        self.game.reveal()

        with self.assertRaises(SoupError):
            await self.game.ask(self.client, "もう一つ")

    async def test_empty_and_too_long_questions_are_rejected(self):
        with self.assertRaises(SoupError):
            await self.game.ask(self.client, "   ")
        with self.assertRaises(SoupError):
            await self.game.ask(self.client, "あ" * (MAX_QUESTION_CHARS + 1))

    async def test_question_limit(self):
        self.game.max_questions = 1
        await self.game.ask(self.client, "ひとつ目")

        with self.assertRaises(SoupError):
            await self.game.ask(self.client, "ふたつ目")


class StoreTests(unittest.TestCase):
    def test_bundled_puzzles_load(self):
        puzzles = load_puzzles()
        self.assertGreaterEqual(len(puzzles), 5)

        for puzzle in puzzles.values():
            self.assertTrue(puzzle.problem)
            self.assertTrue(puzzle.solution)
            self.assertTrue(puzzle.hints)

    def test_unknown_game_is_none(self):
        soup = TurtleSoup(FakeD1(), puzzles={"test-puzzle": make_puzzle()})
        self.assertIsNone(soup.get("missing"))
        self.assertIsNone(soup.get(None))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        readings_path = root / "readings.json"
        readings_path.write_text(json.dumps({}), encoding="utf-8")
        self.client = FakeD1()
        self.soup = TurtleSoup(self.client, puzzles={"test-puzzle": make_puzzle()})
        self.dashboard = ReadingDashboard(
            ReadingDictionary(str(readings_path)),
            type("TTS", (), {"calls": []})(),
            password_file=str(root / "dashboard-auth.json"),
            soup=self.soup,
        )
        self.web = TestClient(TestServer(self.dashboard.app))
        await self.web.start_server()

    async def asyncTearDown(self):
        await self.web.close()
        self.temp_dir.cleanup()

    async def test_new_game_state_and_ask(self):
        response = await self.web.post("/api/turtle/new", json={})
        self.assertEqual(response.status, 201)
        payload = await response.json()
        game_id = payload["game"]["id"]
        self.assertEqual(payload["game"]["status"], "playing")
        self.assertEqual(payload["game"]["puzzle"]["problem"], "男が7階で降りた。なぜ？")

        asked = await self.web.post("/api/turtle/ask", json={"game": game_id, "text": "届きますか？"})
        self.assertEqual(asked.status, 200)
        body = await asked.json()
        self.assertEqual(body["game"]["turns"][-1]["label"], "はい")
        self.assertEqual(body["game"]["question_count"], 1)

        state = await self.web.get(f"/api/turtle?game={game_id}")
        self.assertEqual(state.status, 200)
        self.assertEqual((await state.json())["game"]["id"], game_id)

    async def test_guess_solves_and_shows_solution(self):
        created = await (await self.web.post("/api/turtle/new", json={})).json()
        game_id = created["game"]["id"]
        response = await self.web.post("/api/turtle/guess",
                                       json={"game": game_id, "text": "ボタンに届かないから"})
        body = await response.json()
        self.assertEqual(body["game"]["status"], "solved")
        self.assertEqual(body["game"]["solution"], "ボタンに届かなかったから。")

    async def test_unknown_game_is_404(self):
        self.assertEqual((await self.web.get("/api/turtle?game=nope")).status, 404)
        self.assertEqual((await self.web.post("/api/turtle/ask",
                                              json={"game": "nope", "text": "q"})).status, 404)

    async def test_empty_question_is_400(self):
        created = await (await self.web.post("/api/turtle/new", json={})).json()
        response = await self.web.post("/api/turtle/ask",
                                       json={"game": created["game"]["id"], "text": "  "})
        self.assertEqual(response.status, 400)

    async def test_hint_and_reveal(self):
        created = await (await self.web.post("/api/turtle/new", json={})).json()
        game_id = created["game"]["id"]
        hinted = await (await self.web.post("/api/turtle/hint", json={"game": game_id})).json()
        self.assertEqual(hinted["game"]["hints_shown"], ["体に理由がある。"])
        revealed = await (await self.web.post("/api/turtle/reveal", json={"game": game_id})).json()
        self.assertEqual(revealed["game"]["status"], "revealed")
        self.assertIsNotNone(revealed["game"]["solution"])

    async def test_puzzles_list_hides_solutions(self):
        response = await self.web.get("/api/turtle/puzzles")
        body = await response.json()
        self.assertEqual(len(body["puzzles"]), 1)
        self.assertNotIn("solution", body["puzzles"][0])


class D1ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_engine_raises_user_facing_error(self):
        client = D1Client("http://127.0.0.1:9", timeout=0.5)

        with self.assertRaises(Exception) as caught:
            await client.ask({"問題": "q", "真相": "a"},
                             {"x": {"type": "noul", "instructions": "q"}})

        self.assertIn("判定エンジン", str(caught.exception))
        await client.close()


if __name__ == "__main__":
    unittest.main()
