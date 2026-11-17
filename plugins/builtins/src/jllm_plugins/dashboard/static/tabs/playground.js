const PG_GROUPS = [
  { title: "響き", codes: ["ec", "ecl", "rb"] },
  { title: "声", codes: ["ch", "vi", "pi", "wd", "lm"] },
  { title: "質感", codes: ["bs", "lo", "ph", "ex"] },
  { title: "特殊", codes: ["kk", "tc", "rv"] },
];
const PG_EFFECT_TOKEN = /(?:^|[\s　(（)）])(ecl|rb|ec|ch|vi|lo|bs|pi|rv|lm|wd|ph|ex|kk|tc)(\d{1,3})?(?=[\s　)）(（]|$)/gi;
const PG_SPEED_TOKEN = /(?:^|[\s　(（)）])[fF](\d*\.?\d+)?(?=[\s　)）(（]|$)/g;

let ready = false;
let labels = new Map();

function setStatus(panel, message, kind = "") {
  const status = panel.querySelector("#pgStatus");
  status.textContent = message;
  status.className = `pg-status${kind ? ` ${kind}` : ""}`;
}

function parseTokens(value) {
  const found = [];
  let match;
  PG_EFFECT_TOKEN.lastIndex = 0;
  while ((match = PG_EFFECT_TOKEN.exec(value)) !== null) {
    found.push({
      at: match.index,
      kind: "effect",
      code: match[1].toLowerCase(),
      level: match[2] ? Math.min(20, Math.max(1, parseInt(match[2], 10))) : 1,
    });
  }
  PG_SPEED_TOKEN.lastIndex = 0;
  while ((match = PG_SPEED_TOKEN.exec(value)) !== null) {
    found.push({
      at: match.index,
      kind: "speed",
      speed: match[1] ? parseFloat(match[1]) : 1.5,
    });
  }
  return found.sort((a, b) => a.at - b.at);
}

function renderUsage(panel) {
  const value = panel.querySelector("#pgText").value;
  const tokens = parseTokens(value);
  const chips = panel.querySelector("#pgUsageChips");
  chips.replaceChildren();
  for (const token of tokens) {
    const pill = document.createElement("span");
    const code = document.createElement("b");
    const label = document.createElement("i");
    if (token.kind === "speed") {
      pill.className = "pg-pill spd";
      code.textContent = `f${token.speed}`;
      label.textContent = "速さ";
    } else {
      pill.className = "pg-pill";
      code.textContent = `${token.code}${token.level}`;
      label.textContent = labels.get(token.code) || "効果";
    }
    pill.append(code, label);
    chips.append(pill);
  }
  panel.querySelector("#pgUsageEmpty").hidden = tokens.length > 0;
  const used = new Set(tokens.filter((t) => t.kind === "effect").map((t) => t.code));
  panel.querySelectorAll("#pgEffects .pg-chip").forEach((c) => c.classList.toggle("on", used.has(c.dataset.code)));
  const counter = panel.querySelector("#pgCount");
  counter.textContent = `${value.length} / 300`;
  counter.classList.toggle("full", value.length >= 300);
}

export function mount(panel) {
  panel.innerHTML = `
    <article class="card pg-main">
      <div class="card-head pg-head">
        <div><span class="pg-kicker">PLAYGROUND</span><h2>実験場</h2><p>好きな文章に好きな効果を付けて、その場で鳴らせます。</p></div>
        <span class="pg-badge">ログイン不要・保存なし</span>
      </div>
      <div class="pg-body">
        <div class="pg-field">
          <div class="pg-label"><label for="pgText">文章</label><span class="pg-count" id="pgCount">0 / 300</span></div>
          <textarea id="pgText" class="field pg-input" rows="3" maxlength="300" spellcheck="false" placeholder="例：おはよう みんな ec3 bs20"></textarea>
          <div class="pg-usage" id="pgUsage" aria-live="polite">
            <span class="pg-usage-empty" id="pgUsageEmpty">効果を書くと、ここに内容が出ます</span>
            <div class="pg-pills" id="pgUsageChips"></div>
          </div>
        </div>
        <div class="pg-controls">
          <div class="pg-field"><label for="pgVoice">話者</label><select id="pgVoice" class="select"></select></div>
          <div class="pg-field"><label for="pgSpeed">速さ</label><select id="pgSpeed" class="select"><option value="0.8">0.8 ゆっくり</option><option value="1" selected>1.0 ふつう</option><option value="1.3">1.3 はやめ</option><option value="1.6">1.6 はやい</option><option value="2">2.0 とてもはやい</option></select></div>
        </div>
        <div class="pg-field">
          <div class="pg-label"><span>効果</span><span>押すと末尾に付きます（強さ 1〜20）</span></div>
          <div class="pg-palette" id="pgEffects"></div>
        </div>
        <div class="pg-examples">
          <span class="subtle">ためし書き</span>
          <button type="button" class="pg-example" data-example="おはよう みんな ec3">全体にエコー</button>
          <button type="button" class="pg-example" data-example="ここだけ (大きく)bs20 それ以外">一部だけ重低音</button>
          <button type="button" class="pg-example" data-example="きらきら tc10 きらきら">ツリーチャイム</button>
        </div>
        <div class="pg-run-row">
          <button id="pgPlay" class="pg-run" type="button">ためす</button>
          <button id="pgClear" class="button ghost small" type="button">消す</button>
          <span id="pgStatus" class="pg-status" role="status">文章を入れて「ためす」を押してください。</span>
        </div>
        <div class="pg-result" id="pgResult" hidden>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <strong style="font-size:13px">できました</strong>
            <span class="subtle" id="pgMeta"></span>
          </div>
          <audio id="pgAudio" controls preload="metadata"></audio>
        </div>
      </div>
    </article>
    <aside class="side pg-side">
      <article class="card">
        <div class="card-head"><div><h2>掛かる範囲</h2><p>置く位置で対象が変わります。</p></div></div>
        <div class="form-body">
          <ul class="pg-rules">
            <li><code>前半 後半 ec3</code><span>全体に掛かる</span></li>
            <li><code>前半 ec3 後半</code><span>前半だけ</span></li>
            <li><code>前半 (後半)ec3</code><span>後半だけ</span></li>
            <li><code>本文 ec3 bs20</code><span>その順に重ねる</span></li>
          </ul>
        </div>
      </article>
      <article class="card">
        <div class="card-head"><div><h2>この実験場について</h2></div></div>
        <div class="form-body">
          <ul class="pg-facts">
            <li><span>・</span><span>Discordの読み上げと<b>同じ規約</b>で鳴ります。</span></li>
            <li><span>・</span><span>鳴らした音は<b>保存しません</b>。</span></li>
            <li><span>・</span><span>文章は300文字まで、1分8回まで。</span></li>
          </ul>
        </div>
      </article>
    </aside>`;

  const input = panel.querySelector("#pgText");
  input.addEventListener("input", () => renderUsage(panel));
  panel.querySelectorAll(".pg-example").forEach((btn) => {
    btn.addEventListener("click", () => {
      input.value = btn.dataset.example || "";
      renderUsage(panel);
      input.focus();
    });
  });

  panel.querySelector("#pgClear").addEventListener("click", () => {
    input.value = "";
    renderUsage(panel);
    panel.querySelector("#pgResult").hidden = true;
    const audio = panel.querySelector("#pgAudio");
    audio.pause();
    audio.removeAttribute("src");
    if (audio.dataset.url) { URL.revokeObjectURL(audio.dataset.url); delete audio.dataset.url; }
    setStatus(panel, "文章を入れて「ためす」を押してください。");
  });

  panel.querySelector("#pgPlay").addEventListener("click", async () => {
    const text = input.value.trim();
    const voice = panel.querySelector("#pgVoice").value;
    const speed = parseFloat(panel.querySelector("#pgSpeed").value) || 1;
    if (!text) { setStatus(panel, "文章を入力してください。", "error"); input.focus(); return; }
    if (!voice) { setStatus(panel, "話者を選んでください。", "error"); return; }
    const button = panel.querySelector("#pgPlay");
    button.disabled = true;
    setStatus(panel, "生成しています…");
    try {
      const resp = await fetch("/api/playground", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice_name: voice, speed }),
      });
      if (!resp.ok) {
        let message = "生成に失敗しました。";
        try { message = (await resp.json()).error || message; } catch (_) {}
        throw new Error(message);
      }
      const blob = await resp.blob();
      const audio = panel.querySelector("#pgAudio");
      if (audio.dataset.url) URL.revokeObjectURL(audio.dataset.url);
      const url = URL.createObjectURL(blob);
      audio.dataset.url = url;
      audio.src = url;
      panel.querySelector("#pgResult").hidden = false;
      const meta = panel.querySelector("#pgMeta");
      const size = `${(blob.size / 1024).toFixed(0)} KB`;
      meta.textContent = size;
      audio.onloadedmetadata = () => {
        if (Number.isFinite(audio.duration)) meta.textContent = `${audio.duration.toFixed(2)} 秒 ・ ${size}`;
      };
      audio.play().catch(() => {});
      setStatus(panel, "再生して確認してください。", "success");
    } catch (error) {
      setStatus(panel, error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
}

export async function activate() {
  if (ready) return;
  const panel = document.getElementById("playgroundPanel");
  setStatus(panel, "効果と話者を読み込んでいます…");
  try {
    const resp = await fetch("/api/playground/effects", { credentials: "same-origin" });
    if (!resp.ok) throw new Error("実験場を読み込めませんでした。");
    const meta = await resp.json();

    const voice = panel.querySelector("#pgVoice");
    voice.replaceChildren();
    for (const name of meta.voices) voice.append(new Option(name, name));
    if (!meta.voices.length) voice.append(new Option("（話者がありません）", ""));

    const known = new Map(meta.effects.map((e) => [e.code, e]));
    labels = new Map(meta.effects.map((e) => [e.code, e.label]));
    const box = panel.querySelector("#pgEffects");
    box.replaceChildren();
    const placed = new Set();
    const groups = PG_GROUPS.map((g) => ({
      title: g.title,
      effects: g.codes.map((c) => known.get(c)).filter((e) => e && !placed.has(e.code) && placed.add(e.code)),
    }));
    const rest = meta.effects.filter((e) => !placed.has(e.code));
    if (rest.length) groups.push({ title: "その他", effects: rest });

    for (const group of groups) {
      if (!group.effects.length) continue;
      const row = document.createElement("div");
      row.className = "pg-group";
      const title = document.createElement("span");
      title.className = "pg-group-title";
      title.textContent = group.title;
      const chips = document.createElement("div");
      chips.className = "pg-chips";
      for (const eff of group.effects) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pg-chip";
        btn.dataset.code = eff.code;
        btn.title = `${eff.code}：${eff.description}`;
        const code = document.createElement("span");
        code.className = "pg-chip-code";
        code.textContent = eff.code;
        const name = document.createElement("span");
        name.className = "pg-chip-name";
        name.textContent = eff.label;
        btn.append(code, name);
        btn.addEventListener("click", () => {
          const input = panel.querySelector("#pgText");
          input.value = `${input.value.replace(/[ \u3000]+$/, "")} ${eff.code}5`.trimStart();
          renderUsage(panel);
          input.focus();
        });
        chips.append(btn);
      }
      row.append(title, chips);
      box.append(row);
    }

    ready = true;
    setStatus(panel, "文章を入れて「ためす」を押してください。");
    renderUsage(panel);
  } catch (error) {
    setStatus(panel, error.message, "error");
  }
}
