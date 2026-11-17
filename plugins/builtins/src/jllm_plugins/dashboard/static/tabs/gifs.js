const PAGE_SIZE = 30;

let gifs = [];
let limit = PAGE_SIZE;
let loaded = false;

function render(panel, ctx, refresh) {
  const query = panel.querySelector("#gifSearch").value.trim().toLocaleLowerCase();
  const body = panel.querySelector("#gifRows");
  body.replaceChildren();
  const shown = gifs.filter((item) => `${item.trigger} ${item.file}`.toLocaleLowerCase().includes(query));
  const isAdmin = ctx.isAuthenticated();
  const visible = shown.slice(0, limit);
  const frag = document.createDocumentFragment();

  for (const entry of visible) {
    const tile = document.createElement("div");
    tile.className = "asset-tile tile";
    const title = document.createElement("div");
    title.className = "asset-title";
    title.textContent = entry.trigger;
    const meta = document.createElement("div");
    meta.className = "asset-meta";
    meta.textContent = entry.file;

    const preview = document.createElement("div");
    preview.className = "asset-preview";
    const image = document.createElement("img");
    image.src = `/api/gifs/file?trigger=${encodeURIComponent(entry.trigger)}`;
    image.alt = `${entry.trigger} のGIF`;
    image.loading = "lazy";
    preview.append(image);
    tile.append(title, meta, preview);

    if (isAdmin) {
      const actions = document.createElement("div");
      actions.className = "asset-controls";
      actions.append(
        ctx.makeButton("編集", "secondary", () => ctx.openEditor("gif", entry, refresh)),
        ctx.makeButton("削除", "danger", async () => {
          if (!window.confirm(`「${entry.trigger}」をGIF辞書から削除しますか？`)) return;
          try {
            await ctx.api("/api/gifs", { method: "DELETE", body: JSON.stringify({ trigger: entry.trigger }) });
            await refresh();
            await ctx.refreshSummary();
            ctx.notice("#globalNotice", `「${entry.trigger}」を辞書から削除しました。`, "success");
          } catch (error) {
            ctx.notice("#globalNotice", error.message);
          }
        }),
      );
      tile.append(actions);
    }
    frag.append(tile);
  }

  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "一致するGIFの登録はありません。";
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
  document.getElementById("gifCount").textContent = String(gifs.length);
  document.getElementById("gifTabCount").textContent = `(${gifs.length})`;
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>登録済みのGIF</h2></div>
        <div class="list-tools"><input id="gifSearch" class="search" type="search" aria-label="GIF辞書を検索" placeholder="語句を検索"></div>
      </div>
      <div id="gifRows" class="tile-grid"></div>
    </article>
    <aside class="side admin-only">
      <article class="card">
        <div class="card-head"><div><h2>GIFを登録</h2><p>同じ語句ならGIFを差し替えます。</p></div></div>
        <form id="gifUploadForm" class="form-body">
          <div class="field-group"><label class="field-label" for="gifTrigger">語句</label><input id="gifTrigger" class="field" maxlength="80" required placeholder="例：おめでとう"></div>
          <div class="field-group"><label class="field-label" for="gifFile">GIFファイル</label><input id="gifFile" class="field" type="file" accept=".gif,image/gif" required><p class="inline-hint">GIF、10MB以内。登録時に既存のBot設定で圧縮します。</p></div>
          <button class="button" type="submit" style="width:100%">GIFを登録</button>
          <div id="gifNotice" class="notice" role="status"></div>
        </form>
      </article>
    </aside>`;

  const refresh = async () => {
    gifs = (await (await ctx.api("/api/gifs")).json()).entries || [];
    render(panel, ctx, refresh);
  };
  panel._refreshGifs = refresh;
  panel._renderGifs = () => render(panel, ctx, refresh);

  panel.querySelector("#gifSearch").addEventListener("input", ctx.debounce(() => {
    limit = PAGE_SIZE;
    render(panel, ctx, refresh);
  }, 150));

  panel.querySelector("#gifUploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    ctx.clearNotice("#gifNotice");
    const form = new FormData();
    form.append("trigger", panel.querySelector("#gifTrigger").value.trim());
    form.append("file", panel.querySelector("#gifFile").files[0]);
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await ctx.api("/api/gifs/upload", { method: "POST", body: form });
      event.currentTarget.reset();
      await refresh();
      await ctx.refreshSummary();
      ctx.notice("#gifNotice", "GIFを登録しました。", "success");
    } catch (error) {
      ctx.notice("#gifNotice", error.message);
    } finally {
      button.disabled = false;
    }
  });
}

export async function activate(ctx) {
  const panel = document.getElementById("gifPanel");
  if (!loaded || ctx.isAuthenticated() !== panel._lastAdmin) {
    loaded = true;
    panel._lastAdmin = ctx.isAuthenticated();
    await panel._refreshGifs();
  } else {
    panel._renderGifs();
  }
}
