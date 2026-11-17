let listenNames = {};
let duckCurve = null;
let duckDrag = -1;
let pollTimer = null;

function listenRow(title, meta) {
  const row = document.createElement("div");
  row.className = "asset-row";
  const info = document.createElement("div");
  info.style.minWidth = "0";
  const head = document.createElement("div");
  head.className = "asset-title";
  head.textContent = title;
  const sub = document.createElement("div");
  sub.className = "asset-meta";
  sub.textContent = meta;
  info.append(head, sub);
  row.append(info);
  return row;
}

function curveSummary(curve) {
  const points = (curve && curve.points) || [];
  return points.length ? points.map((p) => `${p.seconds}秒 ${Math.round(p.volume * 100)}%`).join(" → ") : "";
}

function drawCurve(panel, curve) {
  const canvas = panel.querySelector("#listenCurve");
  if (!canvas || !curve?.samples?.length) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 900;
  const height = 240;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const c2d = canvas.getContext("2d");
  c2d.setTransform(ratio, 0, 0, ratio, 0, 0);
  c2d.clearRect(0, 0, width, height);

  const samples = [...curve.samples].reverse();
  const span = curve.seconds || 30;
  const x = (ago) => width - (ago / span) * width;
  const y = (v) => height - 18 - Math.max(0, Math.min(1, v)) * (height - 40);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#f59e0b";

  c2d.strokeStyle = "rgba(255,255,255,.11)";
  c2d.fillStyle = "rgba(244,244,245,.58)";
  c2d.font = "11px system-ui, sans-serif";
  c2d.lineWidth = 1;
  for (const v of [0, 0.5, 1]) {
    c2d.beginPath();
    c2d.moveTo(0, y(v));
    c2d.lineTo(width, y(v));
    c2d.stroke();
    c2d.fillText(`${Math.round(v * 100)}%`, 4, y(v) - 4);
  }
  for (const [key, alpha] of [["receiving", 0.08], ["active", 0.18]]) {
    c2d.fillStyle = `rgba(34,197,94,${alpha})`;
    for (const sample of samples) {
      if (!sample[key]) continue;
      const left = Math.min(x(sample.ago), x(sample.ago - (curve.tick || 0.04)));
      c2d.fillRect(left, 12, Math.max(1.5, (width / span) * (curve.tick || 0.04)), height - 30);
    }
  }
  c2d.beginPath();
  c2d.strokeStyle = accent;
  c2d.lineWidth = 2.5;
  samples.forEach((s, i) => {
    const px = x(s.ago);
    const py = y(s.volume);
    if (i) c2d.lineTo(px, py);
    else c2d.moveTo(px, py);
  });
  c2d.stroke();

  const timings = curve.timings || {};
  const parts = [];
  if (timings.trigger_delay !== undefined) parts.push(`反応まで ${timings.trigger_delay}秒`);
  if (timings.duck_time !== undefined) parts.push(`下がりきるまで ${timings.duck_time}秒`);
  if (timings.release_start !== undefined) parts.push(`保持 ${curve.hold_seconds}秒後に戻し始め`);
  if (timings.release_time !== undefined) parts.push(`戻りきるまで ${timings.release_time}秒`);
  panel.querySelector("#listenCurveMeta").textContent = `いま ${Math.round((curve.samples[0].volume ?? 1) * 100)}%`;
  const summary = curveSummary(curve.curve);
  const settings = summary ? `設定カーブ: ${summary} / 保持 ${curve.hold_seconds}秒` : `設定: 反応まで ${curve.trigger_seconds}秒 / 保持 ${curve.hold_seconds}秒`;
  panel.querySelector("#listenCurveTimings").textContent = parts.length ? `直近の1回: ${parts.join(" / ")}（${settings}）` : `まだ下げていません（${settings}）`;
}

function duckSpan() {
  const last = Math.max(...duckCurve.points.map((p) => p.seconds), 1);
  return Math.max(5, Math.ceil(last + 2));
}

function duckXY(canvas, point) {
  const width = canvas.clientWidth || 900;
  const height = 240;
  const pad = 34;
  return { x: pad + (point.seconds / duckSpan()) * (width - pad - 12), y: height - 24 - point.volume * (height - 48) };
}

function duckFromXY(canvas, clientX, clientY) {
  const box = canvas.getBoundingClientRect();
  const pad = 34;
  const height = 240;
  const seconds = ((clientX - box.left - pad) / (box.width - pad - 12)) * duckSpan();
  const volume = (height - 24 - (clientY - box.top)) / (height - 48);
  return { seconds: Math.max(0, Math.min(60, seconds)), volume: Math.max(0, Math.min(1, volume)) };
}

function drawDuckEditor(panel) {
  const canvas = panel.querySelector("#duckEditor");
  if (!canvas || !duckCurve) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 900;
  const height = 240;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const c2d = canvas.getContext("2d");
  c2d.setTransform(ratio, 0, 0, ratio, 0, 0);
  c2d.clearRect(0, 0, width, height);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#f59e0b";
  const pad = 34;

  c2d.strokeStyle = "rgba(255,255,255,.11)";
  c2d.fillStyle = "rgba(244,244,245,.58)";
  c2d.font = "11px system-ui, sans-serif";
  c2d.lineWidth = 1;
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    const y = height - 24 - v * (height - 48);
    c2d.beginPath();
    c2d.moveTo(pad, y);
    c2d.lineTo(width - 12, y);
    c2d.stroke();
    c2d.fillText(`${Math.round(v * 100)}%`, 4, y - 4);
  }
  const span = duckSpan();
  for (let s = 0; s <= span; s += Math.max(1, Math.round(span / 6))) {
    const x = pad + (s / span) * (width - pad - 12);
    c2d.beginPath();
    c2d.moveTo(x, 12);
    c2d.lineTo(x, height - 24);
    c2d.stroke();
    c2d.fillText(`${s}s`, x + 2, height - 8);
  }
  const points = [...duckCurve.points].sort((a, b) => a.seconds - b.seconds);
  c2d.fillStyle = "rgba(245,158,11,.13)";
  points.forEach((p, i) => {
    const from = duckXY(canvas, p);
    const to = i + 1 < points.length ? duckXY(canvas, points[i + 1]) : { x: width - 12 };
    c2d.fillRect(from.x, from.y, Math.max(2, to.x - from.x), height - 24 - from.y);
  });
  c2d.beginPath();
  c2d.strokeStyle = accent;
  c2d.lineWidth = 2.5;
  points.forEach((p, i) => {
    const { x, y } = duckXY(canvas, p);
    if (i) c2d.lineTo(x, y);
    else c2d.moveTo(x, y);
  });
  c2d.stroke();
  points.forEach((p, i) => {
    const { x, y } = duckXY(canvas, p);
    c2d.beginPath();
    c2d.fillStyle = accent;
    c2d.arc(x, y, i === duckDrag ? 8 : 6, 0, Math.PI * 2);
    c2d.fill();
    c2d.fillStyle = "#fff";
    c2d.beginPath();
    c2d.arc(x, y, 2.5, 0, Math.PI * 2);
    c2d.fill();
  });
}

function renderDuckEditor(panel, ctx) {
  duckCurve.points.sort((a, b) => a.seconds - b.seconds);
  drawDuckEditor(panel);
  const box = panel.querySelector("#duckPoints");
  box.replaceChildren();
  duckCurve.points.forEach((point, index) => {
    const row = document.createElement("div");
    row.className = "curve-point";
    const dot = document.createElement("span");
    dot.className = "dot";
    const seconds = document.createElement("input");
    seconds.className = "field";
    seconds.type = "number";
    seconds.min = "0";
    seconds.max = "60";
    seconds.step = "0.1";
    seconds.value = point.seconds;
    seconds.addEventListener("change", () => {
      point.seconds = Math.max(0, Math.min(60, Number(seconds.value) || 0));
      renderDuckEditor(panel, ctx);
    });
    const volume = document.createElement("input");
    volume.className = "field";
    volume.type = "number";
    volume.min = "0";
    volume.max = "100";
    volume.step = "1";
    volume.value = Math.round(point.volume * 100);
    volume.addEventListener("change", () => {
      point.volume = Math.max(0, Math.min(1, (Number(volume.value) || 0) / 100));
      renderDuckEditor(panel, ctx);
    });
    const remove = ctx.makeButton("削除", "ghost", () => {
      if (duckCurve.points.length <= 1) return;
      duckCurve.points.splice(index, 1);
      renderDuckEditor(panel, ctx);
    });
    row.append(dot, seconds, "秒 →", volume, "%", remove);
    box.append(row);
  });

  const sorted = [...duckCurve.points].sort((a, b) => a.seconds - b.seconds);
  const first = sorted.length ? sorted[0].volume : 1;
  const changed = sorted.slice(1).find((p) => p.volume < first - 0.005);
  panel.querySelector("#duckReaction").value = changed ? changed.seconds : 0;
  panel.querySelector("#duckAttack").value = duckCurve.attack;
  panel.querySelector("#duckRelease").value = duckCurve.release;
  panel.querySelector("#duckHold").value = duckCurve.hold_seconds;
  panel.querySelector("#duckAttackValue").textContent = `${Math.round(duckCurve.attack * 100)}%（1tickあたり）`;
  panel.querySelector("#duckReleaseValue").textContent = `${Math.round(duckCurve.release * 100)}%（1tickあたり）`;
}

async function refreshListening(panel) {
  const resp = await fetch("/api/listening", { credentials: "same-origin" });
  if (!resp.ok) return;
  const data = await resp.json();
  Object.assign(listenNames, data.names || {});
  const curveCard = panel.querySelector("#listenCurveCard");
  if (curveCard) {
    const duck = data.authenticated && data.guilds.length ? (data.guilds[0].duck || {}) : {};
    curveCard.hidden = !duck.curve;
    if (duck.curve) drawCurve(panel, duck.curve);
  }
  const box = panel.querySelector("#listenState");
  box.replaceChildren();
  if (!data.watching) {
    box.innerHTML = '<div class="empty">VCに接続していません（/join すると聞き取りが始まります）。</div>';
    return;
  }
  for (const guild of data.guilds) {
    const duck = guild.duck || {};
    const names = (duck.speakers || []).map((id) => listenNames[id] || id).filter(Boolean);
    const summary = curveSummary((duck.curve || {}).curve);
    const state = duck.enabled
      ? `${duck.active ? "喋っている" : "静か"} · いまの音量 ${Math.round((duck.volume ?? 1) * 100)}%`
        + (duck.speaking_seconds ? ` · ${duck.speaking_seconds}秒` : "")
        + (summary ? ` · カーブ: ${summary}` : `（反応まで ${duck.trigger_seconds}秒）`)
      : "無効（DUCK_ENABLED=false）";
    box.append(listenRow("空気読み", `${state}${names.length ? ` · ${names.join("、")}` : ""}`));
  }
}

export function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>いまの聞き取り</h2><p>VCで喋っている人の声を、空気読みがどう扱っているかです（表示中のみ1秒更新）。</p></div>
        <button id="listenRefresh" class="button secondary small" type="button">更新</button>
      </div>
      <div id="listenState"><div class="empty">読み込んでいます…</div></div>
    </article>
    <article class="card admin-only" id="listenCurveCard" hidden>
      <div class="card-head"><div><h2>音量カーブ</h2><p>直近30秒の読み上げ音量推移です。</p></div><span id="listenCurveMeta" class="subtle"></span></div>
      <div class="curve-legend"><span><i style="background:var(--accent)"></i>読み上げ音量</span><span><i style="background:rgba(34,197,94,.45)"></i>喋っている区間</span></div>
      <div class="curve-wrap"><canvas id="listenCurve" width="900" height="240" role="img" aria-label="読み上げ音量の推移"></canvas></div>
      <div id="listenCurveTimings" class="subtle" style="padding:0 20px 14px"></div>
    </article>
    <article class="card admin-only" id="duckEditorCard" hidden>
      <div class="card-head"><div><h2>音量カーブの設定</h2><p>点をドラッグするか数値で調整してください。</p></div><button id="duckSave" class="button small" type="button">保存して反映</button></div>
      <div class="curve-wrap"><canvas id="duckEditor" width="900" height="240" role="img" aria-label="音量カーブの編集"></canvas></div>
      <div class="curve-form">
        <div class="curve-points" id="duckPoints"></div>
        <div class="curve-tools">
          <button id="duckAdd" class="button secondary small" type="button">点を追加</button>
          <button id="duckReset" class="button ghost small" type="button">既定に戻す</button>
        </div>
        <div class="curve-sliders">
          <label>反応するまで <input id="duckReaction" class="field" type="number" min="0" max="60" step="0.1"><span class="subtle">秒</span></label>
          <label>下げる速さ <input id="duckAttack" class="field" type="range" min="0.02" max="1" step="0.01"><span id="duckAttackValue" class="subtle"></span></label>
          <label>戻す速さ <input id="duckRelease" class="field" type="range" min="0.02" max="1" step="0.01"><span id="duckReleaseValue" class="subtle"></span></label>
          <label>喋り終わって保持 <input id="duckHold" class="field" type="number" min="0" max="10" step="0.1"><span class="subtle">秒</span></label>
        </div>
        <p id="duckNotice" class="notice" role="status"></p>
      </div>
    </article>`;

  panel.querySelector("#listenRefresh").addEventListener("click", () => {
    refreshListening(panel).catch((err) => ctx.notice("#globalNotice", err.message));
  });

  panel.querySelector("#duckAdd").addEventListener("click", () => {
    const last = duckCurve.points.reduce((m, p) => Math.max(m, p.seconds), 0);
    duckCurve.points.push({ seconds: Math.min(60, Math.round((last + 2) * 10) / 10), volume: Math.max(0, duckCurve.points[duckCurve.points.length - 1].volume - 0.1) });
    renderDuckEditor(panel, ctx);
  });
  panel.querySelector("#duckReset").addEventListener("click", async () => {
    const resp = await fetch("/api/duck-curve", { credentials: "same-origin" });
    const data = await resp.json();
    duckCurve = { points: data.defaults.points.map((p) => ({ ...p })), attack: data.defaults.attack, release: data.defaults.release, hold_seconds: data.defaults.hold_seconds };
    renderDuckEditor(panel, ctx);
    ctx.notice("#duckNotice", "既定に戻しました（保存すると反映されます）。");
  });

  for (const [id, key] of [["#duckAttack", "attack"], ["#duckRelease", "release"]]) {
    panel.querySelector(id).addEventListener("input", (e) => {
      duckCurve[key] = Number(e.currentTarget.value);
      panel.querySelector(`${id}Value`).textContent = `${Math.round(duckCurve[key] * 100)}%（1tickあたり）`;
      drawDuckEditor(panel);
    });
  }
  panel.querySelector("#duckHold").addEventListener("change", (e) => {
    duckCurve.hold_seconds = Math.max(0, Math.min(10, Number(e.currentTarget.value) || 0));
  });
  panel.querySelector("#duckReaction").addEventListener("change", (e) => {
    const wanted = Math.max(0, Math.min(60, Number(e.currentTarget.value) || 0));
    const points = [...duckCurve.points].sort((a, b) => a.seconds - b.seconds);
    const idx = points.findIndex((p) => p.volume < (points[0]?.volume ?? 1) - 0.005);
    const target = idx >= 0 ? points[idx] : (points[points.length - 1] || null);
    if (target) target.seconds = wanted;
    renderDuckEditor(panel, ctx);
  });
  panel.querySelector("#duckSave").addEventListener("click", async () => {
    const btn = panel.querySelector("#duckSave");
    btn.disabled = true;
    try {
      const resp = await ctx.api("/api/duck-curve", { method: "POST", body: JSON.stringify(duckCurve) });
      const data = await resp.json();
      duckCurve = { points: data.curve.points.map((p) => ({ ...p })), attack: data.curve.attack, release: data.curve.release, hold_seconds: data.curve.hold_seconds };
      renderDuckEditor(panel, ctx);
      ctx.notice("#duckNotice", `保存しました（${data.applied}台の再生に反映）。`, "success");
      refreshListening(panel).catch(() => {});
    } catch (error) {
      ctx.notice("#duckNotice", error.message);
    } finally {
      btn.disabled = false;
    }
  });

  const canvas = panel.querySelector("#duckEditor");
  canvas.addEventListener("pointerdown", (e) => {
    if (!duckCurve) return;
    const at = duckFromXY(canvas, e.clientX, e.clientY);
    let nearest = -1;
    let best = 1e9;
    duckCurve.points.forEach((p, i) => {
      const { x, y } = duckXY(canvas, p);
      const d = Math.hypot(x - (e.clientX - canvas.getBoundingClientRect().left), y - (e.clientY - canvas.getBoundingClientRect().top));
      if (d < best) { best = d; nearest = i; }
    });
    duckDrag = nearest;
    if (nearest >= 0) { duckCurve.points[nearest] = at; renderDuckEditor(panel, ctx); canvas.setPointerCapture(e.pointerId); }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (duckDrag < 0 || !duckCurve) return;
    duckCurve.points[duckDrag] = duckFromXY(canvas, e.clientX, e.clientY);
    renderDuckEditor(panel, ctx);
  });
  canvas.addEventListener("pointerup", () => { duckDrag = -1; if (duckCurve) renderDuckEditor(panel, ctx); });
  window.addEventListener("resize", () => { if (duckCurve && !panel.hidden) drawDuckEditor(panel); });

  document.addEventListener("visibilitychange", () => {
    if (!panel.hidden && !document.hidden) refreshListening(panel).catch(() => {});
  });
}

export async function activate(ctx) {
  const panel = document.getElementById("listenPanel");
  refreshListening(panel).catch(() => {});
  if (ctx.isAuthenticated()) {
    try {
      const resp = await fetch("/api/duck-curve", { credentials: "same-origin" });
      if (resp.ok) {
        const data = await resp.json();
        duckCurve = { points: data.curve.points.map((p) => ({ ...p })), attack: data.curve.attack, release: data.curve.release, hold_seconds: data.curve.hold_seconds };
        panel.querySelector("#duckEditorCard").hidden = false;
        renderDuckEditor(panel, ctx);
      }
    } catch (_) {}
  } else {
    panel.querySelector("#duckEditorCard").hidden = true;
  }

  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!panel.hidden && !document.hidden) refreshListening(panel).catch(() => {});
  }, 1000);
}

export function deactivate() {
  clearInterval(pollTimer);
  pollTimer = null;
}
