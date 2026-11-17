import { avatarNode, chatMarkup, sessionTime } from "../markdown.js";

let ready = false;
let cache = [];
let total = 0;
let current = "";
let assistant = { name: "jllm-voice", avatar_url: "" };

const sessionLabel = (session) => session.title || `${session.parent_user_name || "だれか"} の会話`;

function renderList(panel) {
  const box = panel.querySelector("#sessionList");
  const query = panel.querySelector("#sessionSearch").value.trim().toLowerCase();
  box.replaceChildren();
  const rows = cache.filter((s) => !query || [sessionLabel(s), s.parent_user_name, s.channel_name, s.channel_id]
    .filter(Boolean).join(" ").toLowerCase().includes(query));
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = cache.length ? "一致する会話がありません。" : "まだ会話がありません。";
    box.append(empty);
    return;
  }
  if (rows.length < total) {
    const note = document.createElement("p");
    note.className = "chat-anon";
    note.textContent = `新しい ${rows.length}件を表示しています（全 ${total}件）。`;
    box.append(note);
  }
  for (const session of rows) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `chat-item${session.session_id === current ? " active" : ""}`;
    const title = document.createElement("span");
    title.className = "chat-item-title";
    title.textContent = sessionLabel(session);
    const sub = document.createElement("span");
    sub.className = "chat-item-sub";
    const who = document.createElement("span");
    who.textContent = `${session.channel_name ? `#${session.channel_name}` : `ch ${session.channel_id}`} ・ ${session.parent_user_name || "?"}`;
    const when = document.createElement("span");
    when.textContent = session.parent_session ? "フォーク" : sessionTime(session.updated_at).slice(5);
    if (session.parent_session) when.className = "chat-item-fork";
    sub.append(who, when);
    item.append(title, sub);
    item.addEventListener("click", () => openSession(panel, session.session_id));
    box.append(item);
  }
}

async function openSession(panel, sessionId) {
  current = sessionId;
  renderList(panel);
  const head = panel.querySelector("#chatHead");
  const turns = panel.querySelector("#chatTurns");
  head.hidden = false;
  panel.querySelector("#chatTitle").textContent = "読み込んでいます…";
  panel.querySelector("#chatMeta").textContent = "";
  panel.querySelector("#chatBadge").hidden = true;
  turns.replaceChildren();
  try {
    const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { credentials: "same-origin" });
    if (!resp.ok) throw new Error("会話を読み込めませんでした。");
    const data = await resp.json();
    const session = data.session || {};
    if (data.assistant) assistant = data.assistant;
    panel.querySelector("#chatTitle").textContent = sessionLabel(session);
    const meta = [];
    if (session.channel_name) meta.push(`#${session.channel_name}`);
    if (session.parent_user_name) meta.push(`開始：${session.parent_user_name}`);
    meta.push(`${session.turns}往復`);
    if (session.model) meta.push(session.model);
    meta.push(`更新 ${sessionTime(session.updated_at)}`);
    panel.querySelector("#chatMeta").textContent = meta.join(" ・ ");
    if (session.parent_session) {
      const badge = panel.querySelector("#chatBadge");
      badge.hidden = false;
      badge.textContent = `フォーク元 ${session.parent_session}`;
    }

    const list = data.turns || [];
    const profiles = data.users || {};
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "この会話にはまだ記録がありません。";
      turns.append(empty);
      return;
    }
    for (const turn of list) {
      const bot = turn.role === "assistant";
      const row = document.createElement("div");
      row.className = `chat-row ${bot ? "bot" : "user"}`;
      const profile = bot ? { name: assistant.name, avatar_url: assistant.avatar_url } : (profiles[String(turn.user_id || "")] || {});
      const avatar = avatarNode(turn.user_id, profile.name || turn.user_name, bot, profile.avatar_url);
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      const who = document.createElement("div");
      who.className = "chat-who";
      const strong = document.createElement("strong");
      strong.textContent = bot ? (assistant.name || "jllm-voice") : (turn.user_name || `ID ${turn.user_id}`);
      const time = document.createElement("span");
      time.textContent = sessionTime(turn.created_at);
      who.append(strong, time);
      const text = document.createElement("div");
      text.className = "chat-text";
      text.innerHTML = chatMarkup(turn.content);
      bubble.append(who, text);
      if (turn.truncated) {
        const note = document.createElement("div");
        note.className = "subtle";
        note.textContent = "（長いため先頭のみ表示）";
        bubble.append(note);
      }
      row.append(avatar, bubble);
      turns.append(row);
    }
    turns.scrollTop = turns.scrollHeight;
  } catch (error) {
    panel.querySelector("#chatTitle").textContent = "読み込めませんでした";
    panel.querySelector("#chatMeta").textContent = error.message;
  }
}

async function loadSessions(panel, force = false) {
  if (ready && !force) return;
  const box = panel.querySelector("#sessionList");
  box.innerHTML = '<div class="empty">読み込んでいます…</div>';
  try {
    const resp = await fetch("/api/sessions", { credentials: "same-origin" });
    if (!resp.ok) throw new Error("会話を読み込めませんでした。");
    const data = await resp.json();
    cache = data.sessions || [];
    ready = true;
    total = data.total || cache.length;
    if (data.assistant) assistant = data.assistant;
    document.getElementById("sessionsTabCount").textContent = total ? String(total) : "";
    panel.querySelector("#sessionAnonNote").hidden = !data.anonymous;
    renderList(panel);
  } catch (error) {
    box.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = error.message;
    box.append(empty);
  }
}

export function mount(panel) {
  panel.innerHTML = `
    <div class="chat-layout">
      <aside class="card chat-side">
        <div class="chat-side-head">
          <input id="sessionSearch" class="search" type="search" placeholder="相手・チャンネルで絞る" aria-label="会話を絞り込む">
          <button id="sessionsRefresh" class="button secondary small" type="button">更新</button>
        </div>
        <p id="sessionAnonNote" class="chat-anon" hidden>未ログインのため、名前は匿名で表示しています。</p>
        <div id="sessionList" class="chat-list"><div class="empty">読み込んでいます…</div></div>
      </aside>
      <article class="card chat-main">
        <div class="chat-head" id="chatHead" hidden>
          <div class="chat-head-text"><h2 id="chatTitle">会話</h2><p id="chatMeta"></p></div>
          <span class="chat-badge" id="chatBadge"></span>
        </div>
        <div id="chatTurns" class="chat-turns"><div class="empty">一覧から会話を選んでください。</div></div>
      </article>
    </div>`;

  panel.querySelector("#sessionsRefresh").addEventListener("click", () => loadSessions(panel, true));
  panel.querySelector("#sessionSearch").addEventListener("input", () => renderList(panel));
}

export async function activate() {
  await loadSessions(document.getElementById("sessionsPanel"));
}
