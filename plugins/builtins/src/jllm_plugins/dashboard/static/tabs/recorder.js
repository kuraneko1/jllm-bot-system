const DEFAULT_SCRIPTS = [
  { label: "【穏やか・朝の情景】", text: "おはようございます。窓の外はまだ薄暗く、静かな街がゆっくり目を覚ましています。" },
  { label: "【明るい・期待】", text: "おっ、動きました！　昨日まで失敗していたプログラムが、一発で動いています。" },
  { label: "【困惑・予想外】", text: "……あれ？　さっきまでここにあったファイルがありません。どこへ行ったのでしょう。" },
  { label: "【焦り・緊迫】", text: "まずい、残り三分です。お願いだから、今だけは止まらないでください。" },
  { label: "【怒り・苛立ち】", text: "いや、それはおかしいでしょう！　三時間調べた原因が、たった一文字の入力ミスでした。" },
  { label: "【呆れ・軽い笑い】", text: "再起動したら、普通に直りました。……あの調査時間はいったい何だったのでしょうか。" },
  { label: "【不安・静かな恐怖】", text: "午前三時。誰も触っていないのに画面が光り、カーソルがゆっくり動きました。" },
  { label: "【低声・SF／人工知能】", text: "西暦二〇三五年。人工知能は、個人のコンピューターの中でも静かに動き続けていました。" },
  { label: "【無機質・システム音声】", text: "システムチェック開始。演算装置、正常。ネットワーク、正常。すべての機能が利用可能です。" },
  { label: "【静かな決意・エンディング】", text: "何度失敗しても、もう一度試します。昨日できなかったことが、今日はできるかもしれません。" },
];

const PRESETS = {
  expressive: { title: "感情・場面のバリエーション（元の台本）", parts: DEFAULT_SCRIPTS },
  short: {
    title: "短め・自然な日常会話",
    parts: [
      { label: "落ち着いた挨拶", text: "こんにちは。今日は少し早く起きたので、窓を開けて空気を入れ替えました。" },
      { label: "明るい会話", text: "このお店、前から気になっていたんです。今度の週末に、一緒に行ってみませんか。" },
      { label: "疑問と驚き", text: "あれ、もうこんな時間ですか。話していると、時間が過ぎるのが早いですね。" },
      { label: "穏やかな締め", text: "今日はありがとうございました。気をつけて帰ってください。またお話ししましょう。" },
    ],
  },
  narration: {
    title: "落ち着いたナレーション",
    parts: [
      { label: "朝の景色", text: "朝の光がカーテンを通して差し込みます。静かな部屋に、遠くの鳥の声が聞こえてきました。" },
      { label: "街の様子", text: "駅前の通りには、買い物をする人や仕事へ向かう人が行き交っています。風は少し冷たく感じられます。" },
      { label: "手順の説明", text: "最初に画面の設定を開きます。項目を確認したら、保存のボタンを押してください。" },
      { label: "自然の描写", text: "木々の葉がゆっくりと揺れています。雲の間から見える青空は、昨日よりも澄んでいました。" },
      { label: "振り返り", text: "急いでいるときほど、一度立ち止まって考えてみましょう。小さな気づきが、次の一歩につながります。" },
      { label: "締めの案内", text: "これで本日のご案内は終了です。最後までお聞きいただき、ありがとうございました。" },
    ],
  },
  articulation: {
    title: "数字・言葉のバリエーション",
    parts: [
      { label: "数字と時刻", text: "集合は午前九時十五分です。参加者は十二人で、午後三時ごろには解散する予定です。" },
      { label: "曜日と予定", text: "月曜日に準備を始めて、水曜日に確認します。金曜日の夕方までに、結果をお知らせください。" },
      { label: "食べ物", text: "温かいお茶と、焼きたてのパンを用意しました。りんごとみかんも、お好きなだけどうぞ。" },
      { label: "季節", text: "春には花が咲き、夏には緑が濃くなります。秋の風が吹くと、冬の訪れが少しずつ近づきます。" },
      { label: "身近なもの", text: "机の上には、鉛筆とノートがあります。引き出しから、はさみと封筒を取り出してください。" },
      { label: "問いかけ", text: "赤い箱と青い箱、どちらに入れましょうか。大きいほうがよければ、こちらを使ってください。" },
    ],
  },
};

export function mount(panel) {
  panel.innerHTML = `
    <div class="rec-wrap">
      <div class="rec-choice" id="recChoice">
        <button type="button" class="rec-choice-card" id="chooseRecord" aria-pressed="false">
          <span class="rec-choice-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg></span>
          <strong>ボイスを収録する</strong>
          <span class="subtle">台本を読んで録音し、まとめて登録します</span>
        </button>
        <button type="button" class="rec-choice-card" id="chooseUpload" aria-pressed="false">
          <span class="rec-choice-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M8 8l4-4 4 4"/><path d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"/></svg></span>
          <strong>ボイスをアップロードする</strong>
          <span class="subtle">手元の音声ファイルから登録します</span>
        </button>
      </div>
      <p id="recChoiceNote" class="subtle" role="status" aria-live="polite"></p>

      <div class="rec-step" id="recRecordStep" hidden>
        <button type="button" class="rec-back" id="backFromRecord">戻る</button>
        <div class="rec-layout" id="recLayout">
          <aside class="rec-setup" aria-label="収録の準備">
            <section class="vcard" aria-labelledby="scriptHeading">
              <h3 id="scriptHeading">台本を選ぶ</h3>
              <label for="presetSelect">読みたい台本</label>
              <div class="row"><select id="presetSelect"></select><button id="applyPreset" type="button">台本を変更</button></div>
              <div id="scriptNotice" class="vstatus" role="status" aria-live="polite"></div>
              <details>
                <summary>自作・AI生成のJSONを読み込む</summary>
                <p class="hint">JSONファイルまたは貼り付けで読み込みます（1〜40パート）。</p>
                <input id="scriptFile" type="file" accept=".json,application/json">
                <button id="loadScriptFile" type="button">ファイルを読み込む</button>
                <textarea id="scriptJson" rows="5" maxlength="262144" spellcheck="false" placeholder='{"version":1,"title":"私の台本","parts":[{"label":"自然な声","text":"今日はよい天気ですね。"}]}'></textarea>
                <div class="row"><button id="loadScriptText" type="button">貼り付けた台本を読み込む</button><button id="exportScript" type="button">JSONで保存</button></div>
              </details>
              <details style="margin-top:10px">
                <summary>AIに台本を作ってもらう</summary>
                <label for="promptTheme">テーマ・話し方</label><input id="promptTheme" maxlength="300" value="日常会話。落ち着いた声、明るい声、疑問、軽い驚きを自然に含める。">
                <div class="row"><label for="promptParts" style="flex:1">パート数<input id="promptParts" type="number" min="1" max="40" value="6"></label><label for="promptSeconds" style="flex:1">目安（秒）<input id="promptSeconds" type="number" min="10" max="110" value="60"></label></div>
                <textarea id="aiPrompt" rows="8" readonly spellcheck="false"></textarea>
                <button id="copyPrompt" type="button">依頼プロンプトをコピー</button>
                <div id="promptNotice" class="vstatus" role="status"></div>
              </details>
            </section>
            <section class="vcard rec-mic">
              <h3>マイク</h3>
              <label for="micSelect">入力デバイス</label>
              <select id="micSelect" title="マイク"></select>
              <div class="row"><button id="connectMic" type="button">マイクを接続</button></div>
              <div id="gStatus" class="vstatus" role="status">マイクを接続して入力を確認できます。</div>
            </section>
          </aside>
          <div class="rec-stage">
            <section class="vcard vtopbar">
              <div class="progress"><span id="progText">0 / 10 収録済み</span><div class="bar"><div id="progBar"></div></div></div>
              <div class="rail" id="partRail" aria-label="パート一覧"></div>
            </section>
            <div id="scripts"></div>
            <div class="vcard final" hidden>
              <div style="display:flex;justify-content:space-between;align-items:center"><h3>音声を結合・保存</h3><span id="outputCount" class="subtle">未収録</span></div>
              <div id="finalStatus" class="vstatus">録音ができると、ここでまとめて試聴・保存できます。</div>
              <div class="row"><label>パート間の無音<select id="silence"><option value="0">なし</option><option value="0.3" selected>0.3秒</option><option value="0.5">0.5秒</option><option value="1">1.0秒</option></select></label><button id="combineBtn" class="btn-combine" type="button">録音を結合して確認する</button></div>
              <audio id="finalAudio" controls style="display:none;width:100%;margin-top:10px"></audio>
              <a id="finalDl" class="dl" style="display:none;margin-top:8px">結合した音声を保存（WAV）</a>
              <div class="row"><button id="playAll" type="button">録音を順番に試聴</button><button id="clearAll" type="button">録音をすべて消去</button></div>
            </div>
            <details class="vcard rec-request" hidden>
              <summary>話者登録を申請</summary>
              <form id="requestForm">
                <label for="requesterName">申請者名</label><input id="requesterName" maxlength="80" required>
                <label for="reqVoiceName">希望する話者名</label><input id="reqVoiceName" maxlength="80" placeholder="例：Clone Ryu" required>
                <label for="requestNote">管理者へのメモ（任意）</label><textarea id="requestNote" maxlength="500" rows="2"></textarea>
                <button id="requestSubmit" type="submit" class="btn-combine">管理者に登録を申請</button>
                <div id="requestStatus" class="vstatus" role="status"></div>
              </form>
            </details>
          </div>
        </div>
      </div>

      <div class="rec-step" id="recUploadStep" hidden>
        <button type="button" class="rec-back" id="backFromUpload">戻る</button>
        <section class="vcard">
          <h3>ボイスをアップロード</h3>
          <div class="rec-drop" id="cloneDrop" tabindex="0" role="button">
            <strong>ここに音声ファイルをドロップ</strong>
            <span class="subtle">wav / mp3 / m4a / ogg / flac / webm・20MB以内・1〜120秒</span>
            <input id="uploadFile" type="file" accept=".wav,.webm,.mp3,.flac,.ogg,.m4a,.opus,audio/*" hidden>
          </div>
          <div id="cloneDropNote" class="vstatus" role="status"></div>
        </section>
        <form class="vcard" id="uploadRequestForm" hidden>
          <h3>話者登録を申請</h3>
          <div id="uploadSummary" style="margin:8px 0"></div>
          <label for="uploadRequester">申請者名</label><input id="uploadRequester" maxlength="80" required>
          <label for="uploadVoiceName">希望する話者名</label><input id="uploadVoiceName" maxlength="80" placeholder="例：Clone Ryu" required>
          <label for="uploadNote">管理者へのメモ（任意）</label><textarea id="uploadNote" maxlength="500" rows="2"></textarea>
          <button type="submit" class="btn-combine">管理者に登録を申請</button>
          <div id="uploadStatus" class="vstatus" role="status"></div>
        </form>
      </div>
    </div>`;

  let SCRIPTS = PRESETS.expressive.parts.map((p) => ({ ...p }));
  let scriptTitle = PRESETS.expressive.title;
  let combining = false;
  let preparingMic = false;
  let playingAll = false;
  let stream = null;
  let audioCtx = null;
  let mediaRecorder = null;
  let chunks = [];
  let recordingIdx = -1;
  let recStart = 0;
  let recTimer = null;
  let takes = new Array(SCRIPTS.length).fill(null);
  let currentIdx = 0;
  let combinedBlob = null;
  let combinedUrl = null;
  let uploadPick = null;
  let uploadUrl = "";

  const $r = (sel) => panel.querySelector(sel);
  const scriptsEl = $r("#scripts");
  const gStatus = $r("#gStatus");
  const micSelect = $r("#micSelect");
  const combineBtn = $r("#combineBtn");
  const finalAudio = $r("#finalAudio");
  const finalDl = $r("#finalDl");
  const finalStatus = $r("#finalStatus");

  const setFinalStatus = (msg) => { finalStatus.textContent = msg; };
  const fmtTime = (ms) => `${String(Math.floor(ms / 60000)).padStart(2, "0")}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}.${Math.floor((ms % 1000) / 100)}`;

  function invalidateCombined() {
    combinedBlob = null;
    if (uploadUrl) URL.revokeObjectURL(uploadUrl);
    if (combinedUrl) URL.revokeObjectURL(combinedUrl);
    combinedUrl = null;
    finalAudio.removeAttribute("src");
    finalAudio.style.display = "none";
    finalDl.removeAttribute("href");
    finalDl.style.display = "none";
  }

  function renderRail() {
    const railEl = $r("#partRail");
    railEl.replaceChildren();
    SCRIPTS.forEach((s, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `rail-dot${takes[i] ? " done" : ""}${i === currentIdx ? " on" : ""}`;
      b.textContent = String(i + 1);
      b.title = s.label;
      b.disabled = recordingIdx !== -1 || preparingMic || combining || playingAll;
      b.onclick = () => { currentIdx = i; updateProgress(); };
      railEl.append(b);
    });
  }

  function updateProgress() {
    const n = takes.filter(Boolean).length;
    const atFinal = currentIdx === SCRIPTS.length - 1;
    $r(".final").hidden = !atFinal;
    $r(".rec-request").hidden = !atFinal;
    $r(".vcard.final")?.classList.toggle("unlocked", n === SCRIPTS.length);
    const busy = recordingIdx !== -1 || preparingMic || combining || playingAll;
    panel.querySelectorAll(".btn-rec").forEach((b) => { b.disabled = busy; });
    micSelect.disabled = busy;
    $r("#connectMic").disabled = busy;
    combineBtn.disabled = n === 0 || busy;
    $r("#playAll").disabled = n === 0 || busy;
    $r("#clearAll").disabled = n === 0 || busy;
    const sec = takes.reduce((sum, t) => sum + (t ? t.duration / 1000 : 0), 0);
    $r("#outputCount").textContent = n ? `${n}パート · 約${Math.round(sec)}秒` : "未収録";
    $r("#progText").textContent = `${n} / ${SCRIPTS.length} 収録済み`;
    $r("#progBar").style.width = `${(n / SCRIPTS.length) * 100}%`;
    panel.querySelectorAll(".vcard.part").forEach((c, i) => {
      c.classList.toggle("done", Boolean(takes[i]));
      c.classList.toggle("current", i === currentIdx);
    });
    renderRail();
  }

  async function loadMics() {
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const mics = devs.filter((d) => d.kind === "audioinput");
      const cur = micSelect.value;
      micSelect.replaceChildren();
      mics.forEach((m, i) => micSelect.append(new Option(m.label || `マイク ${i + 1}`, m.deviceId)));
      if (cur) micSelect.value = cur;
    } catch (_) {}
  }

  async function initMic(deviceId) {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (audioCtx) { try { await audioCtx.close(); } catch (_) {} audioCtx = null; }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: deviceId ? { deviceId: { exact: deviceId } } : true });
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      await audioCtx.resume();
      $r("#connectMic").textContent = "マイクを再接続";
      gStatus.textContent = "マイクを接続しました。";
      await loadMics();
      return true;
    } catch (e) {
      gStatus.textContent = `マイクが使えません: ${e.message}`;
      return false;
    }
  }

  async function startRec(i) {
    if (recordingIdx !== -1 || combining || preparingMic || playingAll) return;
    preparingMic = true;
    updateProgress();
    try {
      if (!stream && !(await initMic(micSelect.value))) return;
    } finally {
      preparingMic = false;
      updateProgress();
    }
    currentIdx = i;
    updateProgress();
    invalidateCombined();
    chunks = [];
    recordingIdx = i;
    recStart = Date.now();
    try {
      mediaRecorder = new MediaRecorder(stream);
    } catch (e) {
      gStatus.textContent = `録音開始できません: ${e.message}`;
      recordingIdx = -1;
      return;
    }
    const div = scriptsEl.children[i];
    const recB = div.querySelector(".btn-rec");
    const stopB = div.querySelector(".btn-stop");
    const timer = div.querySelector(".timer");
    const st = div.querySelector(".vstatus");
    const au = div.querySelector("audio");
    const sub = div.querySelector(".sub-row");
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
    mediaRecorder.onstop = () => {
      clearInterval(recTimer);
      recB.disabled = false;
      stopB.disabled = true;
      const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
      const url = URL.createObjectURL(blob);
      const dur = Date.now() - recStart;
      if (takes[i]) URL.revokeObjectURL(takes[i].url);
      takes[i] = { blob, url, duration: dur };
      au.src = url;
      au.style.display = "block";
      sub.style.display = "flex";
      st.textContent = `収録済み (${fmtTime(dur)})`;
      div.querySelector(".btn-dl").href = url;
      div.querySelector(".btn-dl").download = `part${i + 1}.webm`;
      recordingIdx = -1;
      recB.textContent = "録り直す";
      updateProgress();
    };
    mediaRecorder.start(250);
    recB.disabled = true;
    stopB.disabled = false;
    recB.textContent = "録音中…";
    st.textContent = "録音中…";
    clearInterval(recTimer);
    recTimer = setInterval(() => { timer.textContent = fmtTime(Date.now() - recStart); }, 100);
    updateProgress();
  }

  function renderScripts() {
    scriptsEl.replaceChildren();
    SCRIPTS.forEach((s, i) => {
      const div = document.createElement("div");
      div.className = "vcard part";
      div.dataset.i = String(i);
      div.innerHTML = `
        <span class="subtle">パート ${i + 1} / ${SCRIPTS.length}</span>
        <div class="label" style="font-weight:700;margin:6px 0"></div>
        <div class="text" contenteditable="true" role="textbox" aria-multiline="true"></div>
        <div class="rec-controls">
          <button class="btn-rec" type="button">録音を開始</button>
          <button class="btn-stop" type="button" disabled>停止</button>
          <span class="timer">00:00.0</span>
          <span class="vstatus">未収録</span>
        </div>
        <audio controls style="display:none;width:100%;margin-top:8px"></audio>
        <div class="row sub-row" style="display:none">
          <button class="btn-del" type="button">消去</button>
          <a class="btn-dl">音声をダウンロード</a>
        </div>
        <div class="part-nav">
          <button type="button" class="btn-prev" ${i === 0 ? "disabled" : ""}>前のパート</button>
          <button type="button" class="btn-next">${i === SCRIPTS.length - 1 ? "音声をまとめる" : "次のパート"}</button>
        </div>`;
      div.querySelector(".label").textContent = s.label;
      div.querySelector(".text").textContent = s.text;
      div.querySelector(".text").addEventListener("input", (e) => { SCRIPTS[i].text = e.currentTarget.innerText; });
      div.querySelector(".btn-rec").onclick = () => startRec(i);
      div.querySelector(".btn-stop").onclick = () => { if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop(); };
      div.querySelector(".btn-prev").onclick = () => { currentIdx = Math.max(0, i - 1); updateProgress(); };
      div.querySelector(".btn-next").onclick = () => {
        if (i === SCRIPTS.length - 1) $r(".final").scrollIntoView({ behavior: "smooth", block: "center" });
        else { currentIdx = i + 1; updateProgress(); }
      };
      div.querySelector(".btn-del").onclick = () => {
        if (takes[i]) URL.revokeObjectURL(takes[i].url);
        invalidateCombined();
        takes[i] = null;
        div.querySelector("audio").style.display = "none";
        div.querySelector(".sub-row").style.display = "none";
        div.querySelector(".vstatus").textContent = "未収録";
        div.querySelector(".timer").textContent = "00:00.0";
        div.querySelector(".btn-rec").textContent = "録音を開始";
        updateProgress();
      };
      scriptsEl.append(div);
    });
    updateProgress();
  }

  function encodeWAV(buffer) {
    const ch = buffer.numberOfChannels;
    const sr = buffer.sampleRate;
    const len = buffer.length * ch * 2 + 44;
    const ab = new ArrayBuffer(len);
    const v = new DataView(ab);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, "RIFF"); v.setUint32(4, len - 8, true); ws(8, "WAVE"); ws(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, ch, true); v.setUint32(24, sr, true);
    v.setUint32(28, sr * ch * 2, true); v.setUint16(32, ch * 2, true); v.setUint16(34, 16, true);
    ws(36, "data"); v.setUint32(40, len - 44, true);
    let off = 44;
    const chans = Array.from({ length: ch }, (_, c) => buffer.getChannelData(c));
    for (let i = 0; i < buffer.length; i++) {
      for (let c = 0; c < ch; c++) {
        const s = Math.max(-1, Math.min(1, chans[c][i]));
        v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        off += 2;
      }
    }
    return new Blob([ab], { type: "audio/wav" });
  }

  combineBtn.onclick = async () => {
    const idxs = takes.map((t, i) => (t ? i : -1)).filter((i) => i >= 0);
    if (!idxs.length) return;
    combining = true;
    updateProgress();
    setFinalStatus("結合中…");
    try {
      const AC = new (window.AudioContext || window.webkitAudioContext)();
      const sil = parseFloat($r("#silence").value || "0");
      const decoded = [];
      for (const i of idxs) {
        const buf = await AC.decodeAudioData((await takes[i].blob.arrayBuffer()).slice(0));
        let mono;
        if (buf.numberOfChannels === 1) mono = buf.getChannelData(0);
        else {
          mono = new Float32Array(buf.length);
          for (let c = 0; c < buf.numberOfChannels; c++) {
            const d = buf.getChannelData(c);
            for (let n = 0; n < buf.length; n++) mono[n] += d[n] / buf.numberOfChannels;
          }
        }
        decoded.push({ sr: buf.sampleRate, data: mono });
      }
      const outSR = AC.sampleRate;
      const parts = decoded.map((d) => {
        if (d.sr === outSR) return d.data;
        const ratio = d.sr / outSR;
        const len = Math.floor(d.data.length / ratio);
        const out = new Float32Array(len);
        for (let i = 0; i < len; i++) {
          const p = i * ratio;
          const i0 = Math.floor(p);
          const f = p - i0;
          out[i] = d.data[i0] * (1 - f) + (d.data[i0 + 1] || 0) * f;
        }
        return out;
      });
      const silLen = Math.floor(sil * outSR);
      const total = parts.reduce((a, p) => a + p.length, 0) + silLen * (parts.length - 1);
      const out = AC.createBuffer(1, total, outSR);
      const od = out.getChannelData(0);
      let pos = 0;
      parts.forEach((p, k) => { od.set(p, pos); pos += p.length; if (k < parts.length - 1) pos += silLen; });
      const wav = encodeWAV(out);
      invalidateCombined();
      combinedBlob = wav;
      combinedUrl = URL.createObjectURL(wav);
      finalAudio.src = combinedUrl;
      finalAudio.style.display = "block";
      finalDl.href = combinedUrl;
      finalDl.download = "combined.wav";
      finalDl.style.display = "block";
      setFinalStatus(`結合完了: ${idxs.length}パート / ${(total / outSR).toFixed(1)}秒 / ${(wav.size / 1024).toFixed(0)}KB`);
      AC.close();
    } catch (e) {
      setFinalStatus(`結合失敗: ${e.message}`);
    } finally {
      combining = false;
      updateProgress();
    }
  };

  $r("#connectMic").onclick = () => initMic(micSelect.value);
  $r("#playAll").onclick = async () => {
    const idxs = takes.map((t, i) => (t ? i : -1)).filter((i) => i >= 0);
    if (!idxs.length) return;
    playingAll = true;
    updateProgress();
    try {
      for (const i of idxs) {
        currentIdx = i;
        updateProgress();
        const au = scriptsEl.children[i].querySelector("audio");
        await new Promise((resolve, reject) => { au.currentTime = 0; au.onended = resolve; au.onpause = resolve; au.onerror = reject; au.play().catch(reject); });
      }
    } finally {
      playingAll = false;
      updateProgress();
    }
  };
  $r("#clearAll").onclick = () => {
    invalidateCombined();
    takes.forEach((t) => { if (t) URL.revokeObjectURL(t.url); });
    takes = new Array(SCRIPTS.length).fill(null);
    renderScripts();
  };

  for (const [key, preset] of Object.entries(PRESETS)) {
    $r("#presetSelect").append(new Option(`${preset.title}（${preset.parts.length}パート）`, key));
  }
  const applyScript = (val) => {
    if ((takes.some(Boolean) || combinedBlob) && !window.confirm("台本を変更すると現在の録音を消去します。変更しますか？")) return;
    takes.forEach((t) => { if (t) URL.revokeObjectURL(t.url); });
    invalidateCombined();
    SCRIPTS = val.parts.map((p) => ({ ...p }));
    scriptTitle = val.title;
    takes = new Array(SCRIPTS.length).fill(null);
    currentIdx = 0;
    renderScripts();
    $r("#scriptNotice").textContent = `「${scriptTitle}」を読み込みました。`;
  };
  const parseScript = (src) => {
    let text = src.trim().replace(/^\uFEFF/, "");
    const fence = text.match(/^```(?:json)?\s*\n?([\s\S]*?)\n?```$/i);
    if (fence) text = fence[1];
    const v = JSON.parse(text);
    if (!v || v.version !== 1 || !v.title || !Array.isArray(v.parts) || !v.parts.length || v.parts.length > 40) {
      throw new Error("version: 1, title, parts(1〜40) が必要です。");
    }
    return { version: 1, title: String(v.title).trim(), parts: v.parts.map((p) => ({ label: String(p.label).trim(), text: String(p.text).trim() })) };
  };
  $r("#applyPreset").onclick = () => applyScript(PRESETS[$r("#presetSelect").value]);
  $r("#loadScriptText").onclick = () => { try { applyScript(parseScript($r("#scriptJson").value)); } catch (e) { $r("#scriptNotice").textContent = e.message; } };
  $r("#loadScriptFile").onclick = async () => {
    try {
      const f = $r("#scriptFile").files?.[0];
      if (!f) throw new Error("JSONファイルを選択してください。");
      applyScript(parseScript(await f.text()));
    } catch (e) {
      $r("#scriptNotice").textContent = e.message;
    }
  };
  $r("#exportScript").onclick = () => {
    const parts = SCRIPTS.map((p, i) => ({ label: p.label, text: scriptsEl.children[i].querySelector(".text").innerText }));
    const blob = new Blob([JSON.stringify({ version: 1, title: scriptTitle, parts }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "voice-script.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  };

  const updatePrompt = () => {
    const parts = Number($r("#promptParts").value);
    const sec = Number($r("#promptSeconds").value);
    const theme = $r("#promptTheme").value.trim() || "自然な日本語の日常会話";
    $r("#aiPrompt").value = `日本語の話者クローン用オリジナル台本を作成してください。\n希望: ${theme}\nパート数: ${parts}\n目安時間: ${sec}秒\n出力形式: {"version":1,"title":"台本名","parts":[{"label":"名前","text":"本文"}]}`;
  };
  ["#promptTheme", "#promptParts", "#promptSeconds"].forEach((id) => $r(id).addEventListener("input", updatePrompt));
  $r("#copyPrompt").onclick = async () => {
    try { await navigator.clipboard.writeText($r("#aiPrompt").value); $r("#promptNotice").textContent = "コピーしました。"; }
    catch (_) { $r("#promptNotice").textContent = "手動でコピーしてください。"; }
  };
  updatePrompt();

  const showStep = (mode) => {
    $r("#recChoice").hidden = mode !== null;
    $r("#recRecordStep").hidden = mode !== "record";
    $r("#recUploadStep").hidden = mode !== "upload";
  };
  $r("#chooseRecord").onclick = () => showStep("record");
  $r("#chooseUpload").onclick = () => showStep("upload");
  $r("#backFromRecord").onclick = () => showStep(null);
  $r("#backFromUpload").onclick = () => showStep(null);

  const drop = $r("#cloneDrop");
  const upInput = $r("#uploadFile");
  const pickUpload = (file) => {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { $r("#cloneDropNote").textContent = "20MB以内にしてください。"; return; }
    uploadPick = file;
    if (uploadUrl) URL.revokeObjectURL(uploadUrl);
    uploadUrl = URL.createObjectURL(file);
    $r("#cloneDropNote").textContent = `「${file.name}」を受け取りました。`;
    const sum = $r("#uploadSummary");
    sum.replaceChildren();
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = uploadUrl;
    sum.append(`${file.name}（${(file.size / 1024 / 1024).toFixed(2)}MB）`, audio);
    $r("#uploadRequestForm").hidden = false;
  };
  drop.onclick = () => upInput.click();
  upInput.onchange = (e) => pickUpload(e.currentTarget.files?.[0]);
  drop.ondragover = (e) => e.preventDefault();
  drop.ondrop = (e) => { e.preventDefault(); pickUpload(e.dataTransfer?.files?.[0]); };

  const submitVoiceRequest = async (formEl, file, filename, statusEl, nameEl, reqEl, noteEl) => {
    const form = new FormData();
    form.append("name", nameEl.value.trim());
    form.append("requester", reqEl.value.trim());
    form.append("note", noteEl.value.trim());
    form.append("file", file, filename);
    const btn = formEl.querySelector("button[type=submit]");
    btn.disabled = true;
    statusEl.textContent = "申請しています…";
    try {
      const resp = await fetch("/api/voice-requests", { method: "POST", body: form, credentials: "same-origin" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "申請に失敗しました");
      statusEl.textContent = `申請を送信しました。申請ID: ${data.id}`;
    } catch (e) {
      statusEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  };

  $r("#uploadRequestForm").onsubmit = (e) => {
    e.preventDefault();
    if (uploadPick) submitVoiceRequest(e.currentTarget, uploadPick, uploadPick.name, $r("#uploadStatus"), $r("#uploadVoiceName"), $r("#uploadRequester"), $r("#uploadNote"));
  };
  $r("#requestForm").onsubmit = (e) => {
    e.preventDefault();
    if (combinedBlob) submitVoiceRequest(e.currentTarget, combinedBlob, "combined.wav", $r("#requestStatus"), $r("#reqVoiceName"), $r("#requesterName"), $r("#requestNote"));
  };

  renderScripts();
  if (navigator.mediaDevices?.getUserMedia) loadMics();
}
