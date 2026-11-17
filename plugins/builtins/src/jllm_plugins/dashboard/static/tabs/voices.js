let previewUrls = [];
let loaded = false;

function ttsRow(item, ctx, loadTts) {
  const row = document.createElement("div");
  row.className = "tts-row";
  row.dataset.active = String(Boolean(item.active));
  const dot = document.createElement("span");
  dot.className = `tts-dot ${item.healthy ? "ok" : "ng"}`;
  dot.setAttribute("aria-hidden", "true");
  const text = document.createElement("div");
  text.className = "tts-text";
  const name = document.createElement("div");
  name.className = "tts-name";
  name.textContent = item.display || item.id;
  const meta = document.createElement("div");
  meta.className = "tts-meta";
  meta.textContent = [item.kind, item.url, item.healthy ? "疎通OK" : `疎通NG（${item.error || "応答なし"}）`].filter(Boolean).join(" ・ ");
  text.append(name, meta);
  row.append(dot, text);

  if (item.active) {
    const badge = document.createElement("span");
    badge.className = "tts-badge on";
    badge.textContent = "使用中";
    row.append(badge);
  } else if (item.preferred) {
    const badge = document.createElement("span");
    badge.className = "tts-badge pref";
    badge.textContent = "優先";
    row.append(badge);
  }

  const button = document.createElement("button");
  button.type = "button";
  button.className = "button secondary small";
  button.textContent = item.active ? "チェック" : "使う";
  button.addEventListener("click", async () => {
    ctx.clearNotice("#ttsNotice");
    try {
      if (item.active) {
        const resp = await ctx.api("/api/tts/check", { method: "POST" });
        renderTts(await resp.json(), ctx, loadTts);
      } else {
        ctx.notice("#ttsNotice", "切り替えています…（最大60秒ほど）", "info");
        const resp = await ctx.api("/api/tts/active", { method: "POST", body: JSON.stringify({ id: item.id }) });
        renderTts(await resp.json(), ctx, loadTts);
      }
    } catch (error) {
      ctx.notice("#ttsNotice", error.message);
    }
  });
  row.append(button);
  return row;
}

function renderTts(data, ctx, loadTts) {
  const box = document.getElementById("ttsRows");
  box.replaceChildren();
  if (!data.router) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = `この構成では1つのエンジンだけを使います（${data.describe || "未設定"}）。`;
    box.append(empty);
    return;
  }
  for (const item of data.backends || []) box.append(ttsRow(item, ctx, loadTts));
  ctx.notice(
    "#ttsNotice",
    data.active === data.preferred ? `いまは ${data.describe}` : `退避中: ${data.describe}`,
    "info",
  );
}

async function refreshLibrary(panel, ctx, refreshAll) {
  const data = await (await ctx.api("/api/voice-library")).json();
  const rows = panel.querySelector("#voiceRows");
  rows.replaceChildren();
  previewUrls.forEach((url) => URL.revokeObjectURL(url));
  previewUrls = [];
  panel.querySelector("#voiceAdd").disabled = !data.supported;
  document.getElementById("voiceTabCount").textContent = String(data.voices.length);

  if (!data.supported) {
    rows.innerHTML = '<div class="empty">現在のTTSエンジンは話者クローンの登録・削除に対応していません。有効・無効は下の「全エンジンの話者」から切り替えられます。</div>';
    return;
  }

  for (const voice of data.voices) {
    const row = document.createElement("div");
    row.className = "asset-tile tile voice-tile";
    const info = document.createElement("div");
    const title = document.createElement("div");
    title.className = "asset-title";
    title.textContent = voice.name;
    const meta = document.createElement("div");
    meta.className = "asset-meta";
    const enabled = voice.enabled !== false;
    meta.textContent = voice.deletable ? `参照音声から作成した話者 · ${enabled ? "有効" : "無効"}` : "標準・設定話者";
    info.append(title, meta);

    const tune = document.createElement("div");
    tune.className = "voice-tune";
    const voiceTune = ctx.getVoiceSettings(voice.name);

    let serverSpeed = (typeof voice.speed === "number" && Number.isFinite(voice.speed)) ? voice.speed : null;
    let serverVolume = (typeof voice.volume === "number" && Number.isFinite(voice.volume)) ? voice.volume : null;
    if (serverSpeed !== null) ctx.setVoiceSettings(voice.name, { speed: serverSpeed });
    if (serverVolume !== null) ctx.setVoiceSettings(voice.name, { volume: serverVolume });

    const speedInput = document.createElement("input");
    speedInput.type = "range";
    speedInput.min = "0.5";
    speedInput.max = "2";
    speedInput.step = "0.05";
    speedInput.value = String(serverSpeed ?? voiceTune.speed);
    speedInput.setAttribute("aria-label", `${voice.name}の生成速度`);
    const speedText = document.createElement("span");
    speedText.className = "speed-value";
    speedText.textContent = Number(speedInput.value).toFixed(2);
    const speedTune = document.createElement("label");
    speedTune.className = "tune-line";
    speedTune.append("速度", speedInput, speedText);

    const volumeInput = document.createElement("input");
    volumeInput.type = "range";
    volumeInput.min = "0";
    volumeInput.max = "300";
    volumeInput.value = String(Math.round((serverVolume ?? voiceTune.volume) * 100));
    volumeInput.setAttribute("aria-label", `${voice.name}の試聴・Discord音量`);
    const volumeIcon = document.createElement("span");
    volumeIcon.innerHTML = ctx.VOL_ICON_SVG || "音量";
    const volumeTune = document.createElement("label");
    volumeTune.className = "tune-line";
    volumeTune.append(volumeIcon, volumeInput);

    const speedState = document.createElement("span");
    speedState.className = "subtle";
    const volumeState = document.createElement("span");
    volumeState.className = "subtle";

    const paintSpeed = () => {
      const cur = Number(speedInput.value);
      speedState.textContent = serverSpeed !== null && Math.abs(serverSpeed - cur) < 1e-9
        ? `Discord反映済み ${cur.toFixed(2)}x`
        : serverSpeed !== null ? `Discord: ${serverSpeed.toFixed(2)}x → 試聴: ${cur.toFixed(2)}x` : "Discord反映なし";
    };
    const paintVolume = () => {
      const cur = Math.round(Number(volumeInput.value));
      volumeState.textContent = serverVolume !== null && Math.round(serverVolume * 100) === cur
        ? `Discord反映済み ${cur}%`
        : serverVolume !== null ? `Discord: ${Math.round(serverVolume * 100)}% → 試聴: ${cur}%` : "Discord反映なし";
    };

    speedInput.addEventListener("input", () => {
      const v = Number(speedInput.value);
      speedText.textContent = v.toFixed(2);
      ctx.setVoiceSettings(voice.name, { speed: v });
      paintSpeed();
    });
    volumeInput.addEventListener("input", () => {
      const v = Number(volumeInput.value) / 100;
      ctx.setVoiceSettings(voice.name, { volume: v });
      const current = info.querySelector("audio");
      if (current) ctx.setAudioVolume(current, v);
      paintVolume();
    });

    paintSpeed();
    paintVolume();

    const speedApply = ctx.makeButton("速度反映", "secondary", async () => {
      speedApply.disabled = true;
      try {
        const resp = await (await ctx.api("/api/voice-library/speed", {
          method: "PUT",
          body: JSON.stringify({ name: voice.name, speed: Number(speedInput.value) }),
        })).json();
        serverSpeed = resp.speed;
        paintSpeed();
      } catch (error) {
        speedState.textContent = `反映失敗: ${error.message}`;
      } finally {
        speedApply.disabled = false;
      }
    });

    const volumeApply = ctx.makeButton("音量反映", "secondary", async () => {
      volumeApply.disabled = true;
      try {
        const resp = await (await ctx.api("/api/voice-library/volume", {
          method: "PUT",
          body: JSON.stringify({ name: voice.name, volume: Number(volumeInput.value) / 100 }),
        })).json();
        serverVolume = resp.volume;
        paintVolume();
      } catch (error) {
        volumeState.textContent = `反映失敗: ${error.message}`;
      } finally {
        volumeApply.disabled = false;
      }
    });

    tune.append(speedTune, speedState, speedApply, volumeTune, volumeState, volumeApply);
    info.append(tune);

    const actions = document.createElement("div");
    actions.className = "table-actions";
    const preview = ctx.makeButton("試聴", "secondary", async () => {
      preview.disabled = true;
      ctx.clearNotice("#voiceNotice");
      try {
        const tuneNow = ctx.getVoiceSettings(voice.name);
        const resp = await ctx.api("/api/generate", {
          method: "POST",
          body: JSON.stringify({ text: "こんにちは。音声のテストをしています。", voice_name: voice.name, speed: tuneNow.speed }),
        });
        const url = URL.createObjectURL(await resp.blob());
        previewUrls.push(url);
        let audio = info.querySelector("audio");
        if (!audio) {
          audio = document.createElement("audio");
          audio.controls = true;
          audio.setAttribute("aria-label", `${voice.name}の試聴`);
          info.append(audio);
        }
        audio.src = url;
        ctx.setAudioVolume(audio, tuneNow.volume);
        audio.play().catch(() => {});
      } catch (error) {
        ctx.notice("#voiceNotice", error.message);
      } finally {
        preview.disabled = !enabled;
      }
    });
    preview.disabled = !enabled;
    actions.append(preview);

    if (voice.deletable) {
      const toggle = ctx.makeButton(enabled ? "無効にする" : "有効にする", "secondary", async () => {
        toggle.disabled = true;
        try {
          await ctx.api("/api/voice-library", { method: "PUT", body: JSON.stringify({ name: voice.name, enabled: !enabled }) });
          await refreshAll();
          ctx.notice("#voiceNotice", `「${voice.name}」を${enabled ? "無効" : "有効"}にしました。`, "success");
        } catch (error) {
          ctx.notice("#voiceNotice", error.message);
          toggle.disabled = false;
        }
      });
      const remove = ctx.makeButton("削除", "danger", async () => {
        if (!window.confirm(`「${voice.name}」を削除しますか？`)) return;
        remove.disabled = true;
        try {
          await ctx.api("/api/voice-library", { method: "DELETE", body: JSON.stringify({ name: voice.name }) });
          await refreshAll();
          ctx.notice("#voiceNotice", `「${voice.name}」を削除しました。`, "success");
        } catch (error) {
          ctx.notice("#voiceNotice", error.message);
          remove.disabled = false;
        }
      });
      actions.append(toggle, remove);
    }

    row.append(info, actions);
    rows.append(row);
  }
}

async function refreshCatalog(panel, ctx, refreshAll) {
  const box = panel.querySelector("#voiceCatalog");
  try {
    const data = await (await ctx.api("/api/voice-catalog")).json();
    box.replaceChildren();
    ctx.clearNotice("#voiceCatalogNotice");
    let tiles = 0;
    for (const engine of data.engines || []) {
      if (engine.manageable && engine.active) continue;
      const head = document.createElement("div");
      head.style.cssText = "grid-column:1/-1;display:flex;gap:.5rem;align-items:baseline;margin:.3rem 0 .1rem";
      const b = document.createElement("b");
      b.textContent = engine.display || engine.id;
      const meta = document.createElement("span");
      meta.className = "subtle";
      meta.textContent = engine.active ? "使用中" : (engine.error ? "接続できません" : `${engine.voices.length}人`);
      head.append(b, meta);
      box.append(head);

      if (engine.error) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.style.gridColumn = "1/-1";
        empty.textContent = engine.error;
        box.append(empty);
        continue;
      }

      for (const voice of engine.voices) {
        const enabled = voice.enabled !== false;
        const tile = document.createElement("div");
        tile.className = "asset-tile tile voice-tile";
        const info = document.createElement("div");
        const title = document.createElement("div");
        title.className = "asset-title";
        title.textContent = voice.name;
        const note = document.createElement("div");
        note.className = "asset-meta";
        note.textContent = `${engine.display || engine.id} · ${enabled ? "有効" : "無効"}`;
        info.append(title, note);
        tile.append(info);

        if (String(voice.name).toLowerCase() !== "none") {
          const actions = document.createElement("div");
          actions.className = "table-actions";
          const toggle = ctx.makeButton(enabled ? "無効にする" : "有効にする", "secondary", async () => {
            toggle.disabled = true;
            try {
              await ctx.api("/api/voice-catalog", { method: "PUT", body: JSON.stringify({ name: voice.name, enabled: !enabled }) });
              await refreshAll();
              ctx.notice("#voiceCatalogNotice", `「${voice.name}」を${enabled ? "無効" : "有効"}にしました（全エンジン共通）。`, "success");
            } catch (error) {
              ctx.notice("#voiceCatalogNotice", error.message);
              toggle.disabled = false;
            }
          });
          actions.append(toggle);
          tile.append(actions);
        }
        box.append(tile);
        tiles += 1;
      }
    }
    if (!tiles) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.style.gridColumn = "1/-1";
      empty.textContent = "ほかのエンジンの話者はありません。";
      box.append(empty);
    }
  } catch (error) {
    box.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = `話者一覧を取得できませんでした: ${error.message}`;
    box.append(empty);
  }
}

export async function mount(panel, ctx) {
  panel.innerHTML = `
    <div style="display:grid;gap:18px">
      <article class="card">
        <div class="card-head"><div><h2>登録済みの話者</h2><p>話者を試聴し、速度・音量の設定や削除ができます。</p></div><button id="voiceRefresh" class="button secondary small" type="button">一覧を更新</button></div>
        <div id="voiceRows" class="tile-grid"></div>
      </article>
      <article class="card">
        <div class="card-head"><div><h2>全エンジンの話者</h2><p>全エンジン共通（<code>disabled_voices.json</code>）で話者の有効・無効を切り替えられます。</p></div><button id="voiceCatalogRefresh" class="button secondary small" type="button">一覧を更新</button></div>
        <div id="voiceCatalog" class="tile-grid"><div class="empty">読み込んでいます…</div></div>
        <p id="voiceCatalogNotice" class="notice" role="status"></p>
      </article>
    </div>
    <aside class="side admin-only">
      <article class="card">
        <div class="card-head"><div><h2>読み上げエンジン</h2><p>複数のTTSバックエンドを切り替えます。</p></div><button id="ttsRefresh" class="button secondary small" type="button">状態を更新</button></div>
        <div id="ttsRows" class="tts-rows"><div class="empty">読み込んでいます…</div></div>
        <p id="ttsNotice" class="notice" role="status"></p>
      </article>
      <article class="card">
        <div class="card-head"><div><h2>音声から話者を追加</h2></div></div>
        <form id="voiceForm" class="form-body">
          <div class="field-group"><label class="field-label" for="voiceName">話者名</label><input id="voiceName" class="field" placeholder="例：Clone Ryu" maxlength="80" required></div>
          <div class="field-group"><label class="field-label" for="voiceFile">参照音声</label><input id="voiceFile" class="field" type="file" accept=".wav,.mp3,.webm,.flac,.ogg,.m4a,.opus" required></div>
          <p class="field-hint">20MB以内、1〜120秒。</p>
          <button id="voiceAdd" class="button" type="submit" disabled>話者を登録</button>
          <div id="voiceNotice" class="notice" role="status"></div>
        </form>
      </article>
    </aside>`;

  const loadTts = async () => {
    try {
      renderTts(await (await ctx.api("/api/tts/backends")).json(), ctx, loadTts);
    } catch (error) {
      ctx.notice("#ttsNotice", error.message);
    }
  };
  const refreshAll = () => Promise.all([
    refreshLibrary(panel, ctx, refreshAll).catch((err) => ctx.notice("#voiceNotice", err.message)),
    refreshCatalog(panel, ctx, refreshAll),
    loadTts(),
  ]);
  panel._refreshAll = refreshAll;

  panel.querySelector("#voiceRefresh").addEventListener("click", refreshAll);
  panel.querySelector("#voiceCatalogRefresh").addEventListener("click", () => refreshCatalog(panel, ctx, refreshAll));
  panel.querySelector("#ttsRefresh").addEventListener("click", loadTts);

  panel.querySelector("#voiceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    ctx.clearNotice("#voiceNotice");
    const file = panel.querySelector("#voiceFile").files?.[0];
    if (!file || file.size > 20 * 1024 * 1024) {
      ctx.notice("#voiceNotice", "20MB以内の音声ファイルを選択してください。");
      return;
    }
    const button = panel.querySelector("#voiceAdd");
    button.disabled = true;
    button.textContent = "登録しています…";
    const form = new FormData();
    form.append("name", panel.querySelector("#voiceName").value.trim());
    form.append("file", file);
    try {
      const data = await (await ctx.api("/api/voice-library/upload", { method: "POST", body: form })).json();
      event.currentTarget.reset();
      await refreshAll();
      ctx.notice("#voiceNotice", `「${data.name}」を登録しました。`, "success");
    } catch (error) {
      ctx.notice("#voiceNotice", error.message);
    } finally {
      button.disabled = false;
      button.textContent = "話者を登録";
    }
  });
}

export async function activate() {
  const panel = document.getElementById("voicePanel");
  if (!loaded) {
    loaded = true;
    await panel._refreshAll();
  }
}
