const GAME_KEY = "turtleSoupGame";
const MEMO_KEY = "turtleSoupMemo";
const STATUS_LABELS = { playing: "プレイ中", solved: "正解", revealed: "答えを見た" };

let payload = null;
let busy = false;
let started = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function syncControls(panel) {
  const playing = Boolean(payload) && payload.game.status === "playing";
  const game = payload ? payload.game : null;
  for (const [id, disabled] of [
    ["#tsAsk", busy || !playing],
    ["#tsQuestion", busy || !playing],
    ["#tsGuessButton", busy || !playing],
    ["#tsGuess", busy || !playing],
    ["#tsHint", busy || !playing || (game ? game.hints_shown.length >= game.hint_total : true)],
    ["#tsReveal", busy || !playing],
    ["#tsNew", busy],
  ]) {
    const node = panel.querySelector(id);
    if (node) node.disabled = disabled;
  }
}

function render(panel) {
  if (!payload) return;
  const game = payload.game;
  const puzzle = game.puzzle;
  panel.querySelector("#tsTitle").textContent = puzzle.title;
  const stars = "★".repeat(puzzle.difficulty) + "☆".repeat(Math.max(0, 3 - puzzle.difficulty));
  const tags = puzzle.tags.length ? ` ・ ${puzzle.tags.join(" / ")}` : "";
  panel.querySelector("#tsMeta").textContent = `難易度 ${stars}${tags}`;
  panel.querySelector("#tsProblem").textContent = puzzle.problem;
  panel.querySelector("#tsTurns").textContent = `質問 ${game.question_count} / ${game.max_questions}`;
  panel.querySelector("#tsHints").textContent = `ヒント ${game.hints_shown.length} / ${game.hint_total}`;
  const status = panel.querySelector("#tsStatus");
  status.textContent = STATUS_LABELS[game.status] || game.status;
  status.className = `ts-chip ${game.status === "playing" ? "live" : "done"}`;

  const log = panel.querySelector("#tsLog");
  log.replaceChildren();
  if (!game.turns.length) {
    log.append(element("div", "empty", "まだ質問はありません。「はい／いいえ」で答えられる質問を送ってください。"));
  }
  for (const turn of game.turns) {
    if (turn.kind === "question" || turn.kind === "guess") {
      const wrapU = element("div", "ts-turn user");
      wrapU.append(element("span", "ts-who", turn.kind === "guess" ? "あなた（推測）" : "あなた"), element("div", "ts-bubble", turn.text));
      const wrapA = element("div", "ts-turn");
      wrapA.append(element("span", "ts-who", "出題者"));
      const box = element("div", `ts-answer ${turn.kind === "guess" ? turn.verdict : turn.answer}`);
      box.append(element("span", "ts-answer-label", turn.label));
      if (typeof turn.probability === "number" && Number.isFinite(turn.probability)) {
        box.append(element("span", "subtle", ` 判定 ${Math.round(turn.probability * 100)}%`));
      }
      wrapA.append(box);
      log.append(wrapU, wrapA);
    } else if (turn.kind === "hint") {
      const wrapS = element("div", "ts-turn");
      wrapS.append(element("span", "ts-who", "ヒント"), element("div", "ts-bubble", turn.text));
      log.append(wrapS);
    }
  }
  if (game.solution) {
    const card = element("div", "ts-solution");
    card.append(element("h3", "", "真相"), element("p", "", game.solution));
    log.append(card);
  }
  log.scrollTop = log.scrollHeight;
  panel.querySelector("#tsAskForm").hidden = game.status !== "playing";
  panel.querySelector("#tsGuessBox").hidden = game.status !== "playing";
  syncControls(panel);
}

async function call(path, body) {
  const resp = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const err = new Error((data && data.error) || `リクエストに失敗しました（${resp.status}）`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

export function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><span class="ts-kicker">LATERAL THINKING</span><h2 id="tsTitle">ウミガメのスープ</h2><p id="tsMeta">読み込んでいます…</p></div>
        <button id="tsNew" class="button secondary small" type="button">別の問題</button>
      </div>
      <div class="ts-toolbar">
        <span class="ts-chip" id="tsTurns">質問 0</span>
        <span class="ts-chip" id="tsHints">ヒント 0</span>
        <span class="ts-chip live" id="tsStatus">プレイ中</span>
      </div>
      <p class="ts-problem" id="tsProblem"></p>
      <div class="ts-log" id="tsLog" aria-live="polite"></div>
      <form class="ts-composer" id="tsAskForm">
        <label class="sr-only" for="tsQuestion">質問</label>
        <input class="field" id="tsQuestion" maxlength="200" autocomplete="off" placeholder="はい／いいえで答えられる質問を書く">
        <button class="button" id="tsAsk" type="submit">質問する</button>
      </form>
      <details class="ts-guess" id="tsGuessBox">
        <summary>答えを言う（真相の核心だと思う内容）</summary>
        <form id="tsGuessForm">
          <textarea class="field" id="tsGuess" maxlength="400" rows="3" placeholder="真相の推測を書く"></textarea>
          <div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="button secondary" id="tsGuessButton" type="submit">判定する</button></div>
        </form>
      </details>
      <p id="tsNotice" class="notice" role="status"></p>
    </article>
    <aside class="side">
      <article class="card">
        <div class="card-head"><div><h2>遊び方</h2><p>出題者は d1（AI）。真相は最後まで伏せられます。</p></div></div>
        <div class="form-body">
          <ul class="ts-rules">
            <li><span class="num">1</span><span>「はい／いいえ」で答えられる質問をします。</span></li>
            <li><span class="num">2</span><span>答えは<b>はい・いいえ・関係ない</b>の3種類。</span></li>
            <li><span class="num">3</span><span>真相の要点を言い当てたら<b>正解</b>です。</span></li>
          </ul>
          <div style="display:flex;gap:8px;margin-top:14px">
            <button id="tsHint" class="button secondary small" type="button">ヒントを見る</button>
            <button id="tsReveal" class="button ghost small" type="button">答えを見る</button>
          </div>
        </div>
      </article>
      <article class="card">
        <div class="card-head"><div><h2>メモ</h2><p>この端末に保存されます</p></div></div>
        <div class="form-body"><textarea id="tsMemo" class="field" rows="6" placeholder="メモ"></textarea></div>
      </article>
    </aside>`;

  const apply = (data) => {
    payload = data;
    try { localStorage.setItem(GAME_KEY, data.game.id); } catch (_) {}
    render(panel);
  };

  const runAction = async (fn) => {
    if (busy) return;
    busy = true;
    syncControls(panel);
    ctx.clearNotice("#tsNotice");
    try {
      await fn();
    } catch (error) {
      ctx.notice("#tsNotice", error.message);
    } finally {
      busy = false;
      syncControls(panel);
    }
  };

  panel._startNew = () => runAction(async () => {
    apply(await call("/api/turtle/new", {}));
    panel.querySelector("#tsGuessBox").open = false;
  });
  panel._apply = apply;

  panel.querySelector("#tsAskForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = panel.querySelector("#tsQuestion");
    const text = input.value.trim();
    if (!text || !payload || payload.game.status !== "playing") return;
    runAction(async () => {
      apply(await call("/api/turtle/ask", { game: payload.game.id, text }));
      input.value = "";
      input.focus();
    });
  });

  panel.querySelector("#tsGuessForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = panel.querySelector("#tsGuess");
    const text = input.value.trim();
    if (!text || !payload || payload.game.status !== "playing") return;
    runAction(async () => {
      const data = await call("/api/turtle/guess", { game: payload.game.id, text });
      input.value = "";
      apply(data);
    });
  });

  panel.querySelector("#tsHint").addEventListener("click", () => {
    if (payload?.game.status === "playing") runAction(async () => apply(await call("/api/turtle/hint", { game: payload.game.id })));
  });
  panel.querySelector("#tsReveal").addEventListener("click", () => {
    if (payload?.game.status === "playing" && window.confirm("答えを見ますか？")) {
      runAction(async () => apply(await call("/api/turtle/reveal", { game: payload.game.id })));
    }
  });
  panel.querySelector("#tsNew").addEventListener("click", () => {
    if (payload?.game.status === "playing" && payload.game.turns.length && !window.confirm("いまの問題をやめて別の問題にしますか？")) return;
    panel._startNew();
  });

  const memo = panel.querySelector("#tsMemo");
  try { memo.value = localStorage.getItem(MEMO_KEY) || ""; } catch (_) {}
  memo.addEventListener("input", () => { try { localStorage.setItem(MEMO_KEY, memo.value); } catch (_) {} });
}

export async function activate(ctx) {
  if (started) return;
  started = true;
  const panel = document.getElementById("turtlePanel");
  let saved = null;
  try { saved = localStorage.getItem(GAME_KEY); } catch (_) {}
  if (saved) {
    try {
      panel._apply(await call(`/api/turtle?game=${encodeURIComponent(saved)}`));
      return;
    } catch (error) {
      if (error.status !== 404) {
        ctx.notice("#tsNotice", error.message);
        started = false;
        return;
      }
    }
  }
  await panel._startNew();
}
