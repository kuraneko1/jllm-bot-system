const PAGE_SIZE = 60;

let entries = [];
let limit = PAGE_SIZE;
let audioUrl = null;
let loaded = false;

function render(panel, ctx, refresh) {
  const query = panel.querySelector("#searchInput").value.trim().toLocaleLowerCase();
  const shown = entries.filter((e) => `${e.trigger} ${e.reading}`.toLocaleLowerCase().includes(query));
  const body = panel.querySelector("#readingRows");
  const empty = panel.querySelector("#emptyState");
  body.replaceChildren();

  document.getElementById("entryCount").textContent = String(entries.length);
  document.getElementById("readingTabCount").textContent = `(${entries.length})`;
  panel.querySelector("#visibleCount").textContent = `${shown.length} 件表示`;
  empty.hidden = shown.length > 0;

  if (!shown.length) {
    empty.textContent = entries.length ? "一致する登録語がありません。" : "登録済みの読みはありません。";
    return;
  }

  const isAdmin = ctx.isAuthenticated();
  const visible = shown.slice(0, limit);
  const frag = document.createDocumentFragment();

  for (const entry of visible) {
    const tile = document.createElement("div");
    tile.className = "tile";
    const trigger = document.createElement("div");
    trigger.className = "t-trigger";
    trigger.textContent = entry.trigger;
    const reading = document.createElement("div");
    reading.className = "t-reading";
    reading.textContent = entry.reading;
    tile.append(trigger, reading);

    if (isAdmin) {
      const actions = document.createElement("div");
      actions.className = "t-actions";
      actions.append(
        ctx.makeButton("編集", "secondary", () => ctx.openEditor("reading", entry, refresh)),
        ctx.makeButton("削除", "danger", async () => {
          if (!window.confirm(`「${entry.trigger}」を辞書から削除しますか？`)) return;
          try {
            await ctx.api("/api/readings", { method: "DELETE", body: JSON.stringify({ trigger: entry.trigger }) });
            await refresh();
            await ctx.refreshSummary();
            ctx.notice("#globalNotice", `「${entry.trigger}」を削除しました。`, "success");
          } catch (error) {
            ctx.notice("#globalNotice", error.message);
          }
        }),
      );
      tile.append(actions);
    }
    frag.append(tile);
  }

  if (shown.length > visible.length) {
    const bar = document.createElement("div");
    bar.className = "pager-bar";
    const info = document.createElement("span");
    info.className = "pager-info";
    info.textContent = `${visible.length} / ${shown.length} 件を表示中`;
    const more = ctx.makeButton(`もっと表示（+${Math.min(PAGE_SIZE, shown.length - visible.length)}件）`, "secondary", () => {
      limit += PAGE_SIZE;
      render(panel, ctx, refresh);
    });
    const allBtn = ctx.makeButton("すべて表示", "ghost", () => {
      limit = shown.length;
      render(panel, ctx, refresh);
    });
    bar.append(info, more, allBtn);
    frag.append(bar);
  }

  body.append(frag);
}

async function refreshVoices(panel, ctx) {
  const select = panel.querySelector("#speakerSelect");
  const status = panel.querySelector("#ttsStatus");
  if (!ctx.isAuthenticated()) return;
  try {
    const data = await (await ctx.api("/api/voices")).json();
    select.replaceChildren();
    let count = 0;
    for (const group of data.speakers || []) {
      for (const style of group.styles || []) {
        const option = document.createElement("option");
        option.value = style.id;
        option.textContent = `${group.name} · ${style.name} (${style.id})`;
        select.append(option);
        count += 1;
      }
    }
    if (!count) throw new Error("話者がありません");
    status.textContent = "接続済み";
  } catch (_) {
    select.replaceChildren();
    const fallback = document.createElement("option");
    fallback.value = "1";
    fallback.textContent = "話者ID 1（既定）";
    select.append(fallback);
    status.textContent = "未接続";
  }
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>登録済みの読み</h2></div>
        <div class="list-tools">
          <span id="visibleCount" class="subtle">— 件</span>
          <input id="searchInput" class="search" type="search" placeholder="登録語を検索" aria-label="登録語を検索">
        </div>
      </div>
      <div class="tile-grid" id="readingRows"></div>
      <div id="emptyState" class="empty" hidden>登録済みの読みはありません。</div>
    </article>
    <aside class="side admin-only">
      <article class="card">
        <div class="card-head"><div><h2>読みを登録</h2><p>同じ登録語を入れると読みを更新します。</p></div></div>
        <form id="addForm" class="form-body">
          <div class="field-group"><label class="field-label" for="triggerInput">登録語</label><input id="triggerInput" class="field" maxlength="100" placeholder="例：DGXspark" required></div>
          <div class="field-group"><label class="field-label" for="readingInput">読み（ひらがな・カタカナ）</label><input id="readingInput" class="field" maxlength="200" placeholder="例：でぃーじーえっくす" required></div>
          <button class="button" type="submit" style="width:100%">＋ 辞書に登録</button>
          <div id="addNotice" class="notice" role="status"></div>
        </form>
      </article>
      <article class="card">
        <div class="card-head"><div><h2>音声生成プレビュー</h2><p>接続: <strong id="ttsStatus">確認中</strong></p></div></div>
        <form id="generateForm" class="form-body">
          <div class="field-group"><label class="field-label" for="sampleText">テスト文章</label><textarea id="sampleText" class="textarea" maxlength="1000" placeholder="例：DGXspark のテスト読み上げです。" required></textarea></div>
          <div class="form-row">
            <div class="field-group" style="margin:0"><label class="field-label" for="speakerSelect">話者</label><select id="speakerSelect" class="select"><option value="1">読み込み中…</option></select></div>
            <div class="field-group" style="margin:0"><label class="field-label" for="speedInput">速度</label><div class="range-line"><input id="speedInput" type="range" min="0.5" max="2" step="0.05" value="1.2"><span id="speedValue" class="speed-value">1.20</span></div></div>
          </div>
          <button id="generateButton" class="button" type="submit" style="width:100%;margin-top:14px"><span>音声を生成</span></button>
          <div id="generateNotice" class="notice" role="status"></div>
          <div id="previewBox" class="preview-box">
            <div class="preview-label">辞書適用後の文章</div>
            <div id="processedText" class="preview-text"></div>
            <audio id="audioPlayer" controls></audio>
            <label class="vol-wrap" title="プレビュー音量">音量<input id="previewVolume" type="range" min="0" max="100" value="100" aria-label="プレビュー音量"></label>
          </div>
        </form>
      </article>
      <article class="card">
        <div class="password-area">
          <details id="passkeySettings">
            <summary>パスキーの登録・管理</summary>
            <p class="inline-hint">指紋・顔認証、端末のPIN、セキュリティキーなどでログインできます。</p>
            <label class="field-label" for="passkeyName" style="margin-top:12px">パスキーの名前</label>
            <input id="passkeyName" class="field" maxlength="64" value="管理者のパスキー" placeholder="例：スマートフォン">
            <button id="passkeyRegister" class="button secondary small" type="button" style="margin-top:12px">パスキーを登録</button>
            <div id="passkeyList" style="margin-top:12px"></div>
            <div id="passkeyNotice" class="notice" role="status"></div>
          </details>
        </div>
      </article>
    </aside>`;

  const refresh = async () => {
    entries = (await (await ctx.api("/api/readings")).json()).entries || [];
    render(panel, ctx, refresh);
  };
  panel._refreshReadings = refresh;
  panel._renderReadings = () => render(panel, ctx, refresh);

  panel.querySelector("#searchInput").addEventListener("input", ctx.debounce(() => {
    limit = PAGE_SIZE;
    render(panel, ctx, refresh);
  }, 150));

  panel.querySelector("#addForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    ctx.clearNotice("#addNotice");
    const trigger = panel.querySelector("#triggerInput").value.trim();
    const reading = panel.querySelector("#readingInput").value.trim();
    try {
      await ctx.api("/api/readings", { method: "POST", body: JSON.stringify({ trigger, reading }) });
      panel.querySelector("#triggerInput").value = "";
      panel.querySelector("#readingInput").value = "";
      await refresh();
      await ctx.refreshSummary();
      ctx.notice("#addNotice", `「${trigger}」を保存しました。`, "success");
    } catch (error) {
      ctx.notice("#addNotice", error.message);
    }
  });

  const audioPlayer = panel.querySelector("#audioPlayer");
  ctx.applyPreviewVolume(audioPlayer);
  const pv = panel.querySelector("#previewVolume");
  pv.value = String(Math.round(ctx.previewVolume() * 100));
  pv.addEventListener("input", (e) => {
    const val = Number(e.currentTarget.value) / 100;
    ctx.savePreviewVolume(val);
    ctx.setAudioVolume(audioPlayer, val);
  });

  panel.querySelector("#speedInput").addEventListener("input", (e) => {
    panel.querySelector("#speedValue").textContent = Number(e.currentTarget.value).toFixed(2);
  });

  panel.querySelector("#generateForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    ctx.clearNotice("#generateNotice");
    panel.querySelector("#previewBox").classList.remove("visible");
    const button = panel.querySelector("#generateButton");
    button.disabled = true;
    button.textContent = "生成しています…";
    try {
      const resp = await ctx.api("/api/generate", {
        method: "POST",
        body: JSON.stringify({
          text: panel.querySelector("#sampleText").value,
          speaker: panel.querySelector("#speakerSelect").value,
          speed: Number(panel.querySelector("#speedInput").value),
        }),
      });
      const audio = await resp.blob();
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(audio);
      audioPlayer.src = audioUrl;
      panel.querySelector("#processedText").textContent = decodeURIComponent(resp.headers.get("X-Processed-Text") || "");
      panel.querySelector("#previewBox").classList.add("visible");
      ctx.notice("#generateNotice", "音声を生成しました。", "success");
    } catch (error) {
      ctx.notice("#generateNotice", error.message);
    } finally {
      button.disabled = false;
      button.textContent = "音声を生成";
    }
  });

  panel.querySelector("#passkeyRegister").addEventListener("click", async () => {
    const button = panel.querySelector("#passkeyRegister");
    button.disabled = true;
    ctx.clearNotice("#passkeyNotice");
    try {
      await ctx.registerNewPasskey(panel.querySelector("#passkeyName"), "#passkeyList", "#passkeyNotice");
    } catch (error) {
      ctx.notice("#passkeyNotice", ctx.passkeyError(error));
    } finally {
      button.disabled = false;
    }
  });
}

export async function activate(ctx) {
  const panel = document.getElementById("readingPanel");
  const admin = ctx.isAuthenticated();
  if (!loaded || admin !== panel._lastAdmin) {
    loaded = true;
    panel._lastAdmin = admin;
    await panel._refreshReadings();
    if (admin) {
      refreshVoices(panel, ctx);
      ctx.refreshPasskeys("#passkeyList", "#passkeyNotice").catch(() => {});
    }
  } else {
    panel._renderReadings();
  }
}
