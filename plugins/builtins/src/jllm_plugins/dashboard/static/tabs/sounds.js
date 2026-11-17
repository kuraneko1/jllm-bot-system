const PAGE_SIZE = 40;

const APPLY_ICONS = {
  pending: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 12V4.5M5 7.5 8 4.5l3 3"/></svg>',
  saving: '<svg class="spin" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M13 8a5 5 0 1 1-1.5-3.5"/></svg>',
  saved: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5 6.5 11.5 12.5 5"/></svg>',
  error: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.5 5.5A5 5 0 1 0 13 9"/><path d="M13 2.8v2.7h-2.7"/></svg>',
};

let sounds = [];
let limit = PAGE_SIZE;
let currentMenu = null;
let loaded = false;

function storedColumns() {
  try {
    const saved = localStorage.getItem("soundColumns");
    if (["1", "2", "3", "4"].includes(saved)) return saved;
  } catch (_) {}
  return "4";
}

function applyColumns(panel, columns, remember = true) {
  const rows = panel.querySelector("#soundRows");
  rows.dataset.columns = columns;
  panel.querySelectorAll("#soundColumns button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.columns === columns));
  });
  if (remember) {
    try { localStorage.setItem("soundColumns", columns); } catch (_) {}
  }
}

function closeMenu(focus = false) {
  if (!currentMenu) return;
  const { menu, trigger } = currentMenu;
  menu.remove();
  trigger.setAttribute("aria-expanded", "false");
  if (focus) trigger.focus();
  currentMenu = null;
}

function openSoundMenu(trigger, entry, fileIndex, ctx, refresh) {
  if (currentMenu && currentMenu.trigger === trigger) {
    closeMenu();
    return;
  }
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "sound-menu";
  menu.setAttribute("role", "menu");
  const filename = entry.files[fileIndex];

  const addItem = (label, onClick) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("role", "menuitem");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      closeMenu();
      onClick();
    });
    menu.append(btn);
  };

  addItem("語句・参照ファイルを編集", () => ctx.openEditor("sound", entry, refresh));
  addItem("音声ファイルを追加", () => {
    const phrase = document.getElementById("soundPhrase");
    const append = document.getElementById("soundAppend");
    if (phrase) phrase.value = entry.phrase;
    if (append) append.checked = true;
    document.getElementById("soundFile")?.focus();
  });
  addItem("ダウンロード", () => {
    const link = document.createElement("a");
    link.href = `/api/sounds/file?phrase=${encodeURIComponent(entry.phrase)}&file=${encodeURIComponent(filename)}`;
    link.download = filename;
    link.click();
  });
  addItem("辞書から削除", async () => {
    if (!window.confirm(`「${entry.phrase}」を音声辞書から削除しますか？`)) return;
    try {
      await ctx.api("/api/sounds", { method: "DELETE", body: JSON.stringify({ phrase: entry.phrase }) });
      await refresh();
      await ctx.refreshSummary();
      ctx.notice("#globalNotice", `「${entry.phrase}」を辞書から削除しました。`, "success");
    } catch (error) {
      ctx.notice("#globalNotice", error.message);
    }
  });

  document.body.append(menu);
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
  trigger.setAttribute("aria-expanded", "true");
  currentMenu = { menu, trigger };
}

function render(panel, ctx, refresh) {
  const query = panel.querySelector("#soundSearch").value.trim().toLocaleLowerCase();
  const author = panel.querySelector("#soundAuthor").value;
  const sort = panel.querySelector("#soundSort").value;
  const body = panel.querySelector("#soundRows");
  body.querySelectorAll("audio").forEach((audio) => audio.pause());
  body.replaceChildren();
  closeMenu();

  const shown = sounds.filter((item) => {
    const itemAuthor = item.author || "登録者不明";
    return (
      `${item.phrase} ${itemAuthor} ${item.files.join(" ")}`.toLocaleLowerCase().includes(query)
      && (!author || itemAuthor === author)
    );
  });

  shown.sort((a, b) => {
    if (sort !== "phrase") {
      const order = (a.author || "登録者不明").localeCompare(b.author || "登録者不明", "ja");
      if (order) return sort === "author-desc" ? -order : order;
    }
    return a.phrase.localeCompare(b.phrase, "ja");
  });

  const isAdmin = ctx.isAuthenticated();
  const visible = shown.slice(0, limit);
  const frag = document.createDocumentFragment();

  for (const entry of visible) {
    const card = document.createElement("div");
    card.className = "asset-tile tile sound-card";

    const nameTile = document.createElement("div");
    nameTile.className = "sound-name";
    const heading = document.createElement("div");
    heading.className = "asset-title";
    heading.textContent = entry.phrase;
    const meta = document.createElement("div");
    meta.className = "asset-meta";
    meta.textContent = `音声 ${entry.files.length}件 · 登録: ${entry.author || "登録者不明"}`;
    nameTile.append(heading, meta);

    const preview = document.createElement("div");
    preview.className = "asset-preview";

    for (const [fileIndex, filename] of entry.files.entries()) {
      const row = document.createElement("div");
      row.className = "sound-file";
      const src = `/api/sounds/file?phrase=${encodeURIComponent(entry.phrase)}&file=${encodeURIComponent(filename)}`;
      const saved = (entry.volumes || {})[filename];
      const startVolume = (typeof saved === "number" && Number.isFinite(saved))
        ? Math.min(3, Math.max(0, saved))
        : ctx.getFileVolume(entry.phrase, filename, ctx.previewVolume());

      const { root: player, audio } = ctx.createMiniPlayer(src, `${entry.phrase}の音声${fileIndex + 1}`, startVolume);
      row.append(player);

      if (isAdmin) {
        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "sound-menu-trigger";
        trigger.setAttribute("aria-haspopup", "menu");
        trigger.setAttribute("aria-expanded", "false");
        trigger.setAttribute("aria-label", `${entry.phrase}の操作`);
        trigger.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.2" fill="currentColor"/><circle cx="8" cy="8" r="1.2" fill="currentColor"/><circle cx="12.5" cy="8" r="1.2" fill="currentColor"/></svg>';
        trigger.addEventListener("click", (e) => {
          e.stopPropagation();
          openSoundMenu(trigger, entry, fileIndex, ctx, refresh);
        });
        row.append(trigger);

        const tail = document.createElement("div");
        tail.className = "sound-volume";
        const slider = ctx.volumeSlider(audio, `${entry.phrase}の音声${fileIndex + 1}の音量`, startVolume);
        const input = slider.querySelector("input");

        const apply = document.createElement("button");
        apply.type = "button";
        apply.className = "sound-apply";
        const applyText = document.createElement("span");
        const percent = document.createElement("span");
        percent.className = "sound-percent";
        const applyIcon = document.createElement("span");
        applyIcon.className = "apply-icon";
        apply.append(applyText, percent, applyIcon);

        const paintState = (modeOverride = null) => {
          const gain = Number(input.value);
          const applied = (entry.volumes || {})[filename];
          const synced = typeof applied === "number" && Number.isFinite(applied) && Math.round(applied * 100) === gain;
          const mode = modeOverride || (synced ? "saved" : "pending");
          apply.dataset.state = mode;
          applyIcon.innerHTML = APPLY_ICONS[mode] || APPLY_ICONS.pending;
          applyText.textContent = { pending: "反映", saving: "反映中", saved: "反映済", error: "再試行" }[mode];
          percent.textContent = `${gain}%`;
        };

        input.addEventListener("input", () => {
          ctx.setFileVolume(entry.phrase, filename, Number(input.value) / 100);
          paintState();
        });
        apply.addEventListener("click", async () => {
          if (apply.dataset.state === "saved") return;
          ctx.clearNotice("#globalNotice");
          apply.disabled = true;
          input.disabled = true;
          paintState("saving");
          try {
            const resp = await ctx.api("/api/sounds", {
              method: "PUT",
              body: JSON.stringify({
                original: entry.phrase,
                phrase: entry.phrase,
                volumes: { [filename]: Number(input.value) / 100 },
              }),
            });
            entry.volumes = (await resp.json()).volumes || {};
            paintState();
          } catch (error) {
            paintState("error");
            ctx.notice("#globalNotice", `反映失敗: ${error.message}`);
          } finally {
            apply.disabled = false;
            input.disabled = false;
          }
        });
        paintState();
        tail.append(slider, apply);
        row.append(tail);
      }

      preview.append(row);
    }

    card.append(nameTile, preview);
    frag.append(card);
  }

  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "一致する音声の登録はありません。";
    frag.append(empty);
  } else if (shown.length > visible.length) {
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
  document.getElementById("soundCount").textContent = String(sounds.length);
  document.getElementById("soundTabCount").textContent = `(${sounds.length})`;
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>登録済みの音声</h2></div>
        <div class="list-tools">
          <select id="soundAuthor" class="select" aria-label="音声の登録者で絞り込む"><option value="">全登録者</option></select>
          <select id="soundSort" class="select" aria-label="音声の並び順">
            <option value="author-asc">登録者順</option>
            <option value="author-desc">登録者の逆順</option>
            <option value="phrase">語句順</option>
          </select>
          <input id="soundSearch" class="search" type="search" aria-label="音声辞書を検索" placeholder="語句・登録者・ファイル">
          <div class="column-control">
            <span class="column-caption" id="columnLabel">表示列数</span>
            <div id="soundColumns" class="column-options" role="group" aria-labelledby="columnLabel">
              <button type="button" data-columns="1" aria-label="1列で表示" aria-pressed="false"><svg viewBox="0 0 18 18" aria-hidden="true"><rect x="2.5" y="3.5" width="13" height="11" rx="2.2"/></svg></button>
              <button type="button" data-columns="2" aria-label="2列で表示" aria-pressed="false"><svg viewBox="0 0 18 18" aria-hidden="true"><rect x="2.5" y="3.5" width="6" height="11" rx="1.8"/><rect x="9.5" y="3.5" width="6" height="11" rx="1.8"/></svg></button>
              <button type="button" data-columns="3" aria-label="3列で表示" aria-pressed="false"><svg viewBox="0 0 18 18" aria-hidden="true"><rect x="2" y="3.5" width="4" height="11" rx="1.5"/><rect x="7" y="3.5" width="4" height="11" rx="1.5"/><rect x="12" y="3.5" width="4" height="11" rx="1.5"/></svg></button>
              <button type="button" data-columns="4" aria-label="4列で表示" aria-pressed="true"><svg viewBox="0 0 18 18" aria-hidden="true"><rect x="1.8" y="3.5" width="3.1" height="11" rx="1.3"/><rect x="5.7" y="3.5" width="3.1" height="11" rx="1.3"/><rect x="9.6" y="3.5" width="3.1" height="11" rx="1.3"/><rect x="13.5" y="3.5" width="3.1" height="11" rx="1.3"/></svg></button>
            </div>
          </div>
        </div>
      </div>
      <div id="soundRows" class="tile-grid" data-columns="4"></div>
    </article>
    <aside class="side admin-only">
      <article class="card">
        <div class="card-head"><div><h2>音声ファイルを登録</h2><p>既存の語句なら音声の差し替え、または追加ができます。</p></div></div>
        <form id="soundUploadForm" class="form-body">
          <div class="field-group"><label class="field-label" for="soundPhrase">語句</label><input id="soundPhrase" class="field" maxlength="100" required placeholder="例：こんにちは"></div>
          <div class="field-group">
            <label class="field-label" for="soundFile">音声ファイル</label>
            <label class="file-picker"><input id="soundFile" type="file" aria-label="登録する音声ファイル" accept=".wav,.mp3,.m4a,.ogg,.flac,audio/*" required><span>ファイルを選択</span><span id="soundFileSelection" class="file-selection">未選択</span></label>
            <p class="inline-hint">wav / mp3 / m4a / ogg / flac、10MB以内。登録時にWAVへ変換します。</p>
          </div>
          <label class="file-check"><input id="soundAppend" type="checkbox"> 既存の語句に音声を追加（ランダム再生）</label>
          <button class="button" type="submit" style="width:100%">音声を登録</button>
          <div id="soundSuccess" class="notice" role="status" hidden></div>
          <div id="soundNotice" class="notice" role="status"></div>
        </form>
      </article>
    </aside>`;

  const refresh = async () => {
    sounds = (await (await ctx.api("/api/sounds")).json()).entries || [];
    const authorSelect = panel.querySelector("#soundAuthor");
    const selected = authorSelect.value;
    authorSelect.replaceChildren(new Option("全登録者", ""));
    const authors = [...new Set(sounds.map((item) => item.author || "登録者不明"))].sort((a, b) => a.localeCompare(b, "ja"));
    for (const author of authors) authorSelect.append(new Option(author, author));
    authorSelect.value = authors.includes(selected) ? selected : "";
    render(panel, ctx, refresh);
  };

  panel._refreshSounds = refresh;
  panel._renderSounds = () => render(panel, ctx, refresh);

  applyColumns(panel, storedColumns(), false);
  panel.querySelectorAll("#soundColumns button").forEach((btn) => {
    btn.addEventListener("click", () => applyColumns(panel, btn.dataset.columns));
  });

  panel.querySelector("#soundSearch").addEventListener("input", ctx.debounce(() => {
    limit = PAGE_SIZE;
    render(panel, ctx, refresh);
  }, 150));
  panel.querySelector("#soundAuthor").addEventListener("change", () => { limit = PAGE_SIZE; render(panel, ctx, refresh); });
  panel.querySelector("#soundSort").addEventListener("change", () => render(panel, ctx, refresh));

  panel.querySelector("#soundFile").addEventListener("change", (event) => {
    const file = event.currentTarget.files?.[0];
    panel.querySelector("#soundFileSelection").textContent = file ? file.name : "未選択";
  });

  panel.querySelector("#soundUploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    ctx.clearNotice("#soundNotice");
    const form = new FormData();
    form.append("phrase", panel.querySelector("#soundPhrase").value.trim());
    form.append("append", String(panel.querySelector("#soundAppend").checked));
    form.append("file", panel.querySelector("#soundFile").files[0]);
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await ctx.api("/api/sounds/upload", { method: "POST", body: form });
      event.currentTarget.reset();
      panel.querySelector("#soundFileSelection").textContent = "未選択";
      await refresh();
      await ctx.refreshSummary();
      ctx.notice("#soundNotice", "登録しました。", "success");
    } catch (error) {
      ctx.notice("#soundNotice", error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (currentMenu && !currentMenu.menu.contains(event.target) && !currentMenu.trigger.contains(event.target)) {
      closeMenu();
    }
  });
  document.addEventListener("scroll", (event) => {
    if (currentMenu && !currentMenu.menu.contains(event.target)) closeMenu();
  }, true);
  window.addEventListener("resize", () => closeMenu());
}

export async function activate(ctx) {
  const panel = document.getElementById("soundPanel");
  if (!loaded || ctx.isAuthenticated() !== panel._lastAdmin) {
    loaded = true;
    panel._lastAdmin = ctx.isAuthenticated();
    await panel._refreshSounds();
  } else {
    panel._renderSounds();
  }
}
