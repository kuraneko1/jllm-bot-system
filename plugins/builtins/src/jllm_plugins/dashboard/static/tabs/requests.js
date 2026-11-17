async function refresh(panel, ctx) {
  const data = await (await ctx.api("/api/voice-requests")).json();
  const rows = panel.querySelector("#requestRows");
  rows.replaceChildren();
  const pending = data.requests.filter((item) => item.status === "pending");
  document.getElementById("requestsTabCount").textContent = pending.length ? `(${pending.length})` : "";
  const sorted = [...data.requests].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
  if (!sorted.length) {
    rows.innerHTML = '<div class="empty">話者登録の申請はありません。</div>';
    return;
  }
  for (const item of sorted) {
    const row = document.createElement("div");
    row.className = "asset-row";
    const info = document.createElement("div");
    info.style.minWidth = "0";
    const title = document.createElement("div");
    title.className = "asset-title";
    title.textContent = item.name;
    const meta = document.createElement("div");
    meta.className = "asset-meta";
    const label = { pending: "承認待ち", approved: "承認済み", rejected: "拒否済み" }[item.status] || item.status;
    meta.textContent = `${label} · 申請者: ${item.requester} · ${new Date(item.created_at).toLocaleString()} · ${item.seconds}秒`;
    const note = document.createElement("p");
    note.textContent = item.note;
    note.style.whiteSpace = "pre-wrap";
    info.append(title, meta, note);

    const actions = document.createElement("div");
    actions.className = "asset-controls";
    if (item.status === "pending") {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = `/api/voice-requests/${encodeURIComponent(item.id)}/file`;
      ctx.applyPreviewVolume(audio);
      info.append(audio, ctx.volumeSlider(audio, `${item.name}の申請音声の音量`));
      for (const [action, text] of [["approve", "承認して登録"], ["reject", "拒否"]]) {
        actions.append(
          ctx.makeButton(text, action === "reject" ? "danger" : "secondary", async () => {
            if (!window.confirm(`「${item.name}」の申請を${action === "approve" ? "承認して登録" : "拒否"}しますか？`)) return;
            actions.querySelectorAll("button").forEach((n) => { n.disabled = true; });
            try {
              await ctx.api(`/api/voice-requests/${encodeURIComponent(item.id)}/review`, {
                method: "POST",
                body: JSON.stringify({ action }),
              });
              await refresh(panel, ctx);
              ctx.notice("#globalNotice", action === "approve" ? "承認し、話者を登録しました。" : "申請を拒否しました。", "success");
            } catch (error) {
              ctx.notice("#globalNotice", error.message);
            } finally {
              actions.querySelectorAll("button").forEach((n) => { n.disabled = false; });
            }
          }),
        );
      }
    }
    row.append(info, actions);
    rows.append(row);
  }
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>話者クローン登録申請</h2><p>音声を確認し、承認すると話者として登録されます。</p></div>
        <button id="requestsRefresh" class="button secondary small" type="button">一覧を更新</button>
      </div>
      <div id="requestRows"></div>
    </article>`;
  panel.querySelector("#requestsRefresh").addEventListener("click", () => {
    refresh(panel, ctx).catch((err) => ctx.notice("#globalNotice", err.message));
  });
}

export async function activate(ctx) {
  const panel = document.getElementById("requestsPanel");
  await refresh(panel, ctx);
}
