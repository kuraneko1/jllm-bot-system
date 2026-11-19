const GENDER_TEXT = { "": "指定なし", female: "女性", male: "男性" };
const BACKCHANNEL_TEXT = { "": "指定なし", low: "少なめ", mid: "普通", high: "多め" };

let editingId = null;
let lastState = null;

function renderGuildRows(panel, data, ctx, refresh) {
  const box = panel.querySelector("#haruGuilds");
  box.replaceChildren();
  const guilds = data.guilds || [];
  if (!guilds.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "現在ボイスチャンネルに接続しているサーバーはありません。Discord で `/haru join` を実行するとここでフォーカス話者を操作できます。";
    box.append(empty);
    return;
  }

  const personas = data.personas || [];
  for (const g of guilds) {
    const row = document.createElement("div");
    row.className = "tts-row";
    row.dataset.active = String(Boolean(g.active));
    row.style.flexWrap = "wrap";

    const dot = document.createElement("span");
    dot.className = `tts-dot ${g.active ? "ok" : "ng"}`;
    dot.setAttribute("aria-hidden", "true");

    const info = document.createElement("div");
    info.className = "tts-text";
    const title = document.createElement("div");
    title.className = "tts-name";
    title.textContent = `${g.guild_name || g.guild_id}${g.channel_name ? ` / #${g.channel_name}` : ""}`;
    const meta = document.createElement("div");
    meta.className = "tts-meta";
    meta.textContent = g.active
      ? `会話中: ${g.persona_name || "Haru"} ・ 聞き取り: ${g.focus_user_name || (g.focus_user_id ? g.focus_user_id : "VC全員（ミックス）")}`
      : `待機中 ・ 既定聞き取り: ${g.focus_user_name || (g.focus_user_id ? g.focus_user_id : "VC全員（ミックス）")}`;
    info.append(title, meta);

    const ctrl = document.createElement("div");
    ctrl.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-left:auto";

    const focusSelect = document.createElement("select");
    focusSelect.className = "field";
    focusSelect.style.minWidth = "170px";
    focusSelect.setAttribute("aria-label", "フォーカス話者");
    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = "VC 全員（ミックス）";
    focusSelect.append(allOpt);
    const seenIds = new Set();
    for (const m of g.members || []) {
      seenIds.add(String(m.id));
      const opt = document.createElement("option");
      opt.value = String(m.id);
      opt.textContent = m.name || String(m.id);
      focusSelect.append(opt);
    }
    if (g.focus_user_id && !seenIds.has(String(g.focus_user_id))) {
      const opt = document.createElement("option");
      opt.value = String(g.focus_user_id);
      opt.textContent = g.focus_user_name ? `${g.focus_user_name} (${g.focus_user_id})` : String(g.focus_user_id);
      focusSelect.append(opt);
    }
    focusSelect.value = g.focus_user_id ? String(g.focus_user_id) : "";

    const applyFocus = ctx.makeButton("フォーカス適用", "secondary", async () => {
      ctx.clearNotice("#haruStateNotice");
      try {
        await ctx.api("/api/haru/focus", {
          method: "POST",
          body: JSON.stringify({
            guild_id: g.guild_id,
            user_id: focusSelect.value || null,
          }),
        });
        ctx.notice("#haruStateNotice", "フォーカス話者を更新しました。", "success");
        await refresh();
      } catch (err) {
        ctx.notice("#haruStateNotice", err.message);
      }
    });
    ctrl.append(focusSelect, applyFocus);

    if (g.active && personas.length) {
      const pSelect = document.createElement("select");
      pSelect.className = "field";
      pSelect.style.minWidth = "150px";
      pSelect.setAttribute("aria-label", "話す相手を切替");
      for (const p of personas) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name;
        pSelect.append(opt);
      }
      if (g.persona_id) pSelect.value = g.persona_id;
      const switchBtn = ctx.makeButton("相手を切替", "secondary", async () => {
        ctx.clearNotice("#haruStateNotice");
        switchBtn.disabled = true;
        try {
          await ctx.api("/api/haru/switch", {
            method: "POST",
            body: JSON.stringify({ guild_id: g.guild_id, persona_id: pSelect.value }),
          });
          ctx.notice("#haruStateNotice", "会話相手を切り替えました。", "success");
          await refresh();
        } catch (err) {
          ctx.notice("#haruStateNotice", err.message);
        } finally {
          switchBtn.disabled = false;
        }
      });
      const stopBtn = ctx.makeButton("会話終了", "danger", async () => {
        ctx.clearNotice("#haruStateNotice");
        try {
          await ctx.api("/api/haru/stop", {
            method: "POST",
            body: JSON.stringify({ guild_id: g.guild_id }),
          });
          ctx.notice("#haruStateNotice", "Haru 会話を終了しました。", "success");
          await refresh();
        } catch (err) {
          ctx.notice("#haruStateNotice", err.message);
        }
      });
      ctrl.append(pSelect, switchBtn, stopBtn);
    }

    row.append(dot, info, ctrl);
    box.append(row);
  }
  ctx.enhanceSelects?.(box);
}

function setFormPersona(panel, persona) {
  editingId = persona ? persona.id : null;
  panel.querySelector("#haruFormTitle").textContent = persona ? `「${persona.name}」を編集` : "話せるひとを追加";
  panel.querySelector("#haruName").value = persona?.name || "";
  panel.querySelector("#haruGender").value = persona?.gender || "";
  panel.querySelector("#haruBackchannel").value = persona?.backchannel ?? "mid";
  panel.querySelector("#haruGuidance").value = String(persona?.guidance_scale ?? 1.5);
  panel.querySelector("#haruPersonaText").value = persona?.persona || "";
  panel.querySelector("#haruOtherName").value = persona?.other_name || "";
  panel.querySelector("#haruRelationship").value = persona?.relationship || "";
  panel.querySelector("#haruSituation").value = persona?.situation || "";
  panel.querySelector("#haruCustomPrompt").value = persona?.custom_prompt || "";
  panel.querySelector("#haruVoiceFile").value = "";
  panel.querySelector("#haruVoiceHint").textContent = persona?.voice_file
    ? `登録済み音声: ${persona.voice_seconds || 0}秒（差し替える場合のみ選択してください）`
    : "3〜180秒の WAV / FLAC / Ogg / MP3 / WebM 等を選択してください。";
  panel.querySelector("#haruSaveBtn").textContent = persona ? "変更を保存" : "話者を登録";
  panel.querySelector("#haruCancelEdit").hidden = !persona;
  ctxSyncSelects(panel);
}

function ctxSyncSelects(panel) {
  panel.querySelectorAll("select").forEach((sel) => {
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function renderPersonas(panel, data, ctx, refresh) {
  const box = panel.querySelector("#haruPersonaList");
  box.replaceChildren();
  const personas = data.personas || [];
  if (!personas.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "まだ「話せるひと」が登録されていません。上のフォームから名前・人物設定・参照音声を登録してください。";
    box.append(empty);
    return;
  }

  for (const item of personas) {
    const card = document.createElement("div");
    card.className = "entry";
    card.style.flexDirection = "column";
    card.style.alignItems = "stretch";
    card.style.gap = "10px";

    const top = document.createElement("div");
    top.style.cssText = "display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap";

    const head = document.createElement("div");
    head.style.minWidth = "0";
    const nameEl = document.createElement("div");
    nameEl.className = "entry-title";
    nameEl.textContent = item.name;

    const meta = document.createElement("div");
    meta.className = "entry-sub";
    const parts = [
      `性別: ${GENDER_TEXT[item.gender || ""] || "指定なし"}`,
      `相づち: ${BACKCHANNEL_TEXT[item.backchannel ?? "mid"] || "普通"}`,
      `guidance: ${item.guidance_scale ?? 1.5}`,
      item.has_voice ? `音声: ${item.voice_seconds || 0}秒` : "音声未設定",
    ];
    meta.textContent = parts.join(" ・ ");
    head.append(nameEl, meta);

    const actions = document.createElement("div");
    actions.className = "entry-actions";
    const editBtn = ctx.makeButton("編集", "secondary", () => {
      setFormPersona(panel, item);
      panel.querySelector("#haruFormCard")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    const delBtn = ctx.makeButton("削除", "danger", async () => {
      if (!confirm(`「${item.name}」を削除しますか？`)) return;
      ctx.clearNotice("#haruListNotice");
      try {
        await ctx.api(`/api/haru/personas/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        if (editingId === item.id) setFormPersona(panel, null);
        await refresh();
        await ctx.refreshSummary?.();
      } catch (err) {
        ctx.notice("#haruListNotice", err.message);
      }
    });
    actions.append(editBtn, delBtn);
    top.append(head, actions);
    card.append(top);

    const summary = item.custom_prompt || item.persona || item.situation || "";
    if (summary) {
      const p = document.createElement("p");
      p.className = "subtle";
      p.style.cssText = "margin:0;white-space:pre-wrap;word-break:break-word;font-size:13px";
      p.textContent = summary.length > 180 ? `${summary.slice(0, 180)}…` : summary;
      card.append(p);
    }

    if (item.voice_url) {
      const player = ctx.createMiniPlayer(`${item.voice_url}?t=${encodeURIComponent(item.updated_at || "")}`, item.name);
      card.append(player);
    }

    box.append(card);
  }
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card" style="grid-column:1 / -1">
      <div class="card-head">
        <div>
          <h2>Haru リアルタイム会話状態 &amp; フォーカス</h2>
          <p id="haruServerBadge" class="subtle">確認中…</p>
        </div>
        <button id="haruRefresh" class="button secondary small" type="button">更新</button>
      </div>
      <div id="haruStateNotice" class="notice" role="status"></div>
      <div id="haruGuilds"></div>
    </article>

    <article class="card" id="haruFormCard">
      <div class="card-head">
        <div>
          <h2 id="haruFormTitle">話せるひとを追加</h2>
          <p>Discord の <code>/haru join</code> で選択できる人物と声を管理します。</p>
        </div>
      </div>
      <form id="haruPersonaForm">
        <div class="field-group">
          <label class="field-label" for="haruName">名前（必須）</label>
          <input id="haruName" class="field" maxlength="80" placeholder="例: ハル" required>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">
          <div class="field-group">
            <label class="field-label" for="haruGender">性別</label>
            <select id="haruGender" class="field">
              <option value="">指定なし</option>
              <option value="female">女性</option>
              <option value="male">男性</option>
            </select>
          </div>
          <div class="field-group">
            <label class="field-label" for="haruBackchannel">相づち</label>
            <select id="haruBackchannel" class="field">
              <option value="low">少なめ</option>
              <option value="mid" selected>普通</option>
              <option value="high">多め</option>
            </select>
          </div>
          <div class="field-group">
            <label class="field-label" for="haruGuidance">Guidance Scale (1.0〜2.0)</label>
            <input id="haruGuidance" class="field" type="number" min="1.0" max="2.0" step="0.05" value="1.5">
          </div>
        </div>
        <div class="field-group">
          <label class="field-label" for="haruVoiceFile">リファレンス音声（3〜180秒）</label>
          <input id="haruVoiceFile" class="field" type="file" accept="audio/*,.wav,.flac,.ogg,.opus,.mp3,.webm,.m4a">
          <p id="haruVoiceHint" class="inline-hint">3〜180秒の WAV / FLAC / Ogg / MP3 / WebM 等を選択してください。</p>
        </div>
        <div class="field-group">
          <label class="field-label" for="haruPersonaText">人物設定</label>
          <textarea id="haruPersonaText" class="field" rows="3" placeholder="例: 明るくて親しみやすいAIアシスタント。簡潔で自然な口調で話す。"></textarea>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px">
          <div class="field-group">
            <label class="field-label" for="haruOtherName">相手の名前（任意）</label>
            <input id="haruOtherName" class="field" maxlength="80" placeholder="空なら通話相手の名前を使用">
          </div>
          <div class="field-group">
            <label class="field-label" for="haruRelationship">関係性（任意）</label>
            <input id="haruRelationship" class="field" maxlength="200" placeholder="例: 親しい友人。">
          </div>
        </div>
        <div class="field-group">
          <label class="field-label" for="haruSituation">状況（任意）</label>
          <input id="haruSituation" class="field" maxlength="400" placeholder="例: 雑談している。">
        </div>
        <details class="field-group">
          <summary class="field-label" style="cursor:pointer">高度な設定: カスタム system_prompt を直書きする（任意）</summary>
          <textarea id="haruCustomPrompt" class="field" rows="4" maxlength="4000" placeholder="入力した場合は上のカード項目の代わりにこちらをそのまま送信します（最大4000文字）"></textarea>
        </details>
        <div id="haruFormNotice" class="notice" role="status"></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button id="haruSaveBtn" class="button" type="submit">話者を登録</button>
          <button id="haruCancelEdit" class="button secondary" type="button" hidden>編集キャンセル</button>
        </div>
      </form>
    </article>

    <article class="card">
      <div class="card-head">
        <div>
          <h2>登録済みの話せるひと</h2>
          <p><code>/haru join</code> の候補に出るキャラクター一覧です。</p>
        </div>
      </div>
      <div id="haruListNotice" class="notice" role="status"></div>
      <div id="haruPersonaList" class="entries"></div>
    </article>
  `;

  async function refresh() {
    if (!ctx.isAuthenticated()) return;
    try {
      const data = await (await ctx.api("/api/haru/state")).json();
      lastState = data;
      const badge = panel.querySelector("#haruServerBadge");
      const countBadge = document.querySelector("#haruTabCount");
      if (countBadge) countBadge.textContent = `(${(data.personas || []).length})`;
      if (!data.supported) {
        badge.textContent = "Haru プラグインが無効です";
      } else {
        badge.textContent = data.server_ready
          ? `Haru サーバ: オンライン ・ 登録 ${(data.personas || []).length} 人`
          : `Haru サーバ: オフライン / 準備中 ・ 登録 ${(data.personas || []).length} 人`;
      }
      renderGuildRows(panel, data, ctx, refresh);
      renderPersonas(panel, data, ctx, refresh);
    } catch (err) {
      ctx.notice("#haruStateNotice", err.message);
    }
  }

  panel._haruRefresh = refresh;

  panel.querySelector("#haruRefresh").addEventListener("click", () => {
    ctx.clearNotice("#haruStateNotice");
    refresh();
  });

  panel.querySelector("#haruCancelEdit").addEventListener("click", () => {
    ctx.clearNotice("#haruFormNotice");
    setFormPersona(panel, null);
  });

  panel.querySelector("#haruPersonaForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    ctx.clearNotice("#haruFormNotice");
    const btn = panel.querySelector("#haruSaveBtn");
    btn.disabled = true;
    try {
      const fd = new FormData();
      fd.append("name", panel.querySelector("#haruName").value);
      fd.append("gender", panel.querySelector("#haruGender").value);
      fd.append("backchannel", panel.querySelector("#haruBackchannel").value);
      fd.append("guidance_scale", panel.querySelector("#haruGuidance").value);
      fd.append("persona", panel.querySelector("#haruPersonaText").value);
      fd.append("other_name", panel.querySelector("#haruOtherName").value);
      fd.append("relationship", panel.querySelector("#haruRelationship").value);
      fd.append("situation", panel.querySelector("#haruSituation").value);
      fd.append("custom_prompt", panel.querySelector("#haruCustomPrompt").value);
      const file = panel.querySelector("#haruVoiceFile").files?.[0];
      if (file) fd.append("voice", file);

      const url = editingId
        ? `/api/haru/personas/${encodeURIComponent(editingId)}`
        : "/api/haru/personas";
      const method = editingId ? "PUT" : "POST";
      await ctx.api(url, { method, body: fd });
      ctx.notice("#haruFormNotice", editingId ? "話者を更新しました。" : "話者を登録しました。", "success");
      setFormPersona(panel, null);
      await refresh();
      await ctx.refreshSummary?.();
    } catch (err) {
      ctx.notice("#haruFormNotice", err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

export async function activate(ctx) {
  const panel = document.getElementById("haruPanel");
  if (panel?._haruRefresh) {
    await panel._haruRefresh();
  }
}
