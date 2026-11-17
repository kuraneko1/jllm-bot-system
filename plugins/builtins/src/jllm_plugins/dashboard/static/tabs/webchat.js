import { avatarNode, chatMarkup, sessionTime } from "../markdown.js";

let ready = false;
let cache = [];
let current = "";
let controller = null;
let epoch = 0;
let creating = null;
let assistant = { name: "jllm-voice", avatar_url: "" };

function renderList(panel, ctx) {
  const box = panel.querySelector("#webchatList");
  const query = panel.querySelector("#webchatSearch").value.trim().toLowerCase();
  box.replaceChildren();
  const rows = cache.filter((s) => !query || (s.title || "新しい会話").toLowerCase().includes(query));
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = cache.length ? "一致する会話がありません。" : "まだ会話がありません。";
    box.append(empty);
    return;
  }
  for (const session of rows) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `chat-item${session.session_id === current ? " active" : ""}`;
    const title = document.createElement("span");
    title.className = "chat-item-title";
    title.textContent = session.title || "新しい会話";
    const sub = document.createElement("span");
    sub.className = "chat-item-sub";
    const count = document.createElement("span");
    count.textContent = session.turns ? `${session.turns}往復` : "新しい会話";
    const when = document.createElement("span");
    when.textContent = sessionTime(session.updated_at).slice(5);
    sub.append(count, when);
    item.append(title, sub);
    item.addEventListener("click", () => openChat(panel, ctx, session.session_id));
    box.append(item);
  }
}

function makeBubble(role, name) {
  const bot = role === "assistant";
  const row = document.createElement("div");
  row.className = `chat-row ${bot ? "bot" : "user"}`;
  const avatar = bot
    ? avatarNode("", assistant.name, true, assistant.avatar_url)
    : avatarNode("", "あなた", false, "");
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const who = document.createElement("div");
  who.className = "chat-who";
  const strong = document.createElement("strong");
  strong.textContent = bot ? (assistant.name || "jllm-voice") : (name || "あなた");
  const time = document.createElement("span");
  time.textContent = sessionTime(new Date().toISOString());
  who.append(strong, time);
  const text = document.createElement("div");
  text.className = "chat-text";
  bubble.append(who, text);
  row.append(avatar, bubble);
  return { row, text, bubble, who };
}

function renderTurns(panel, turns) {
  const box = panel.querySelector("#webchatTurns");
  box.replaceChildren();
  if (!turns.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "下の入力欄に書いて送ると、会話が始まります。";
    box.append(empty);
    return;
  }
  for (const turn of turns) {
    const { row, text, who } = makeBubble(turn.role, turn.user_name);
    text.innerHTML = chatMarkup(turn.content);
    if (turn.created_at) {
      const span = who.querySelector("span");
      if (span) span.textContent = sessionTime(turn.created_at);
    }
    box.append(row);
  }
  box.scrollTop = box.scrollHeight;
}

async function openChat(panel, ctx, sessionId, mineEpoch) {
  const mine = mineEpoch === undefined ? ++epoch : mineEpoch;
  current = sessionId;
  renderList(panel, ctx);
  try {
    const resp = await fetch(`/api/webchat/sessions/${encodeURIComponent(sessionId)}`, { credentials: "same-origin" });
    if (!resp.ok) throw new Error("会話を読み込めませんでした。");
    const data = await resp.json();
    if (mine !== epoch) return;
    if (data.assistant) assistant = data.assistant;
    const session = data.session || {};
    panel.querySelector("#webchatTitle").textContent = session.title || "新しい会話";
    panel.querySelector("#webchatMeta").textContent = [
      session.model || "",
      session.turns ? `${session.turns}往復` : "",
      session.updated_at ? `更新 ${sessionTime(session.updated_at)}` : "",
    ].filter(Boolean).join(" ・ ") || "新しい会話";
    panel.querySelector("#webchatBadge").hidden = true;
    renderTurns(panel, (data.turns || []).map((t) => ({
      role: t.role, user_name: t.user_name, content: t.content, created_at: t.created_at,
    })));
  } catch (error) {
    ctx.notice("#globalNotice", error.message);
  }
}

async function loadList(panel, ctx, force = false) {
  if (ready && !force) return;
  const box = panel.querySelector("#webchatList");
  box.innerHTML = '<div class="empty">読み込んでいます…</div>';
  try {
    const resp = await fetch("/api/webchat/sessions", { credentials: "same-origin" });
    if (resp.status === 401) {
      box.innerHTML = '<div class="empty">AIチャットには管理者ログインが必要です。</div>';
      return;
    }
    if (!resp.ok) throw new Error("会話を読み込めませんでした。");
    const data = await resp.json();
    cache = data.sessions || [];
    if (data.assistant) assistant = data.assistant;
    ready = true;
    renderList(panel, ctx);
  } catch (error) {
    box.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = error.message;
    box.append(empty);
  }
}

export function mount(panel, ctx) {
  panel.innerHTML = `
    <div class="chat-layout">
      <aside class="card chat-side">
        <div class="chat-side-head">
          <button id="webchatNew" class="button small" type="button">＋ 新しい会話</button>
          <input id="webchatSearch" class="search" type="search" placeholder="会話を絞る" aria-label="会話を絞り込む">
        </div>
        <div id="webchatList" class="chat-list"><div class="empty">読み込んでいます…</div></div>
      </aside>
      <article class="card chat-main">
        <div class="chat-head" id="webchatHead">
          <div class="chat-head-text">
            <h2 id="webchatTitle">Webチャット</h2>
            <p id="webchatMeta">新しい会話を始めるか、一覧から選んでください。</p>
          </div>
          <span class="chat-badge" id="webchatBadge" hidden></span>
        </div>
        <div id="webchatTurns" class="chat-turns"><div class="empty">下の入力欄に書いて送ると、会話が始まります。</div></div>
        <form id="webchatForm" class="chat-input">
          <textarea id="webchatText" class="chat-textarea" rows="1" placeholder="メッセージを入力（Enterで送信）" maxlength="2000"></textarea>
          <button id="webchatSend" class="button small" type="submit">送信</button>
          <button id="webchatStop" class="button ghost small" type="button" hidden>停止</button>
        </form>
      </article>
    </div>`;

  const setBusy = (busy) => {
    panel.querySelector("#webchatSend").disabled = busy;
    panel.querySelector("#webchatStop").hidden = !busy;
  };

  const send = async (event) => {
    if (event) event.preventDefault();
    const input = panel.querySelector("#webchatText");
    const text = input.value.trim();
    if (!text) return;
    if (creating) { try { await creating; } catch (_) {} }
    if (!current) {
      try {
        const created = await fetch("/api/webchat/sessions", { method: "POST", credentials: "same-origin" });
        if (!created.ok) throw new Error("新しい会話を作れませんでした。");
        current = (await created.json()).session_id;
      } catch (error) {
        ctx.notice("#globalNotice", error.message);
        return;
      }
    }

    const box = panel.querySelector("#webchatTurns");
    ++epoch;
    if (box.querySelector(".empty")) box.replaceChildren();
    input.value = "";
    input.style.height = "auto";
    const user = makeBubble("user", "あなた");
    user.text.innerHTML = chatMarkup(text);
    box.append(user.row);

    const pending = makeBubble("assistant");
    const typing = document.createElement("div");
    typing.className = "typing";
    typing.innerHTML = "<span></span><span></span><span></span>";
    pending.text.append(typing);
    box.append(pending.row);
    box.scrollTop = box.scrollHeight;

    setBusy(true);
    controller = new AbortController();
    let answer = "";

    try {
      const resp = await fetch(`/api/webchat/sessions/${encodeURIComponent(current)}/stream`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });
      if (!resp.ok) {
        let message = "送信に失敗しました。";
        try { message = (await resp.json()).error || message; } catch (_) {}
        throw new Error(message);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          let eventName = "message";
          let payload = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) payload += line.slice(5).trim();
          }
          if (!payload) continue;
          let data = {};
          try { data = JSON.parse(payload); } catch (_) { continue; }
          if (eventName === "delta") {
            answer += data.text || "";
            pending.text.innerHTML = `${chatMarkup(answer)}<span class="chat-cursor"></span>`;
            box.scrollTop = box.scrollHeight;
          } else if (eventName === "error") {
            throw new Error(data.message || "生成に失敗しました。");
          } else if (eventName === "done") {
            answer = data.content || answer;
            pending.text.innerHTML = chatMarkup(answer);
            panel.querySelector("#webchatMeta").textContent = `${data.model || ""} ・ ${data.elapsed || 0}秒 ・ 更新 ${sessionTime(new Date().toISOString())}`;
          }
        }
      }
      if (!answer) throw new Error("応答がありませんでした。");
      ready = false;
      await loadList(panel, ctx, true);
      await openChat(panel, ctx, current);
    } catch (error) {
      if (error.name === "AbortError") {
        pending.text.textContent = answer ? `${answer}（停止しました）` : "（停止しました）";
      } else {
        pending.text.textContent = "";
        const note = document.createElement("div");
        note.className = "subtle";
        note.textContent = error.message;
        pending.bubble.append(note);
      }
    } finally {
      setBusy(false);
      controller = null;
      box.scrollTop = box.scrollHeight;
    }
  };

  panel.querySelector("#webchatNew").addEventListener("click", () => {
    if (controller) return;
    const mine = ++epoch;
    current = "";
    ready = false;
    panel.querySelector("#webchatTitle").textContent = "Webチャット";
    panel.querySelector("#webchatMeta").textContent = "新しい会話を始めるか、左から選んでください。";
    panel.querySelector("#webchatBadge").hidden = true;
    renderTurns(panel, []);
    panel.querySelector("#webchatText").focus();
    creating = (async () => {
      try {
        const resp = await fetch("/api/webchat/sessions", { method: "POST", credentials: "same-origin" });
        if (!resp.ok) throw new Error("新しい会話を作れませんでした。");
        const data = await resp.json();
        if (mine !== epoch) return;
        current = data.session_id;
        loadList(panel, ctx, true);
      } catch (error) {
        ctx.notice("#globalNotice", error.message);
      } finally {
        creating = null;
      }
    })();
  });

  panel.querySelector("#webchatSearch").addEventListener("input", () => renderList(panel, ctx));
  panel.querySelector("#webchatForm").addEventListener("submit", send);
  panel.querySelector("#webchatStop").addEventListener("click", () => controller?.abort());
  const textInput = panel.querySelector("#webchatText");
  textInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(null);
    }
  });
  textInput.addEventListener("input", (event) => {
    const node = event.currentTarget;
    node.style.height = "auto";
    node.style.height = `${Math.min(180, node.scrollHeight)}px`;
  });
}

export async function activate(ctx) {
  await loadList(document.getElementById("webchatPanel"), ctx);
}
