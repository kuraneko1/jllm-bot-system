let llmStatus = null;

function showBackend(panel) {
  const backend = panel.querySelector("#llmBackend").value;
  const data = llmStatus?.backends[backend] || {};
  panel.querySelector("#llmUrl").value = data.api_url || "";
  panel.querySelector("#llmModel").value = data.model || "";
  panel.querySelector("#llmTranslate").value = data.translate_model || "";
  panel.querySelector("#llmKey").value = "";
  panel.querySelector("#llmRemoveKey").checked = false;
  panel.querySelector("#llmKeyStatus").textContent =
    `${data.key_configured ? "キー設定済み。" : "キー未設定。"}空欄の場合は変更しません。`;
}

async function loadLlm(panel, ctx) {
  try {
    llmStatus = await (await ctx.api("/api/llm/settings")).json();
    panel.querySelector("#llmBackend").value = llmStatus.backend;
    panel.querySelector("#llmActive").textContent = `現在使用中: ${llmStatus.backend}`;
    showBackend(panel);
    ctx.clearNotice("#llmNotice");
  } catch (error) {
    ctx.notice("#llmNotice", error.message);
  }
}

export function mount(panel, ctx) {
  panel.innerHTML = `
    <article class="card">
      <div class="card-head">
        <div><h2>LLM設定</h2><p>バックエンドごとの接続先とモデルを設定します。</p></div>
        <button id="llmRefresh" class="button secondary small" type="button">再読み込み</button>
      </div>
      <form id="llmForm" class="form-body">
        <div class="field-group">
          <label class="field-label" for="llmBackend">編集するバックエンド</label>
          <p id="llmActive" class="inline-hint"></p>
          <select id="llmBackend" class="select">
            <option value="sarashina">sarashina</option>
            <option value="local">local</option>
            <option value="atmos">atmos</option>
            <option value="openai">openai</option>
          </select>
        </div>
        <div class="field-group">
          <label class="field-label" for="llmUrl">API URL</label>
          <input id="llmUrl" class="field" type="url" maxlength="2048">
          <p class="inline-hint">接続先のベースURLのみ（パスは /v1 まで）。</p>
        </div>
        <div class="field-group"><label class="field-label" for="llmModel">会話モデル</label><input id="llmModel" class="field" maxlength="2048" required></div>
        <div class="field-group"><label class="field-label" for="llmTranslate">翻訳モデル</label><input id="llmTranslate" class="field" maxlength="2048"></div>
        <div class="field-group">
          <label class="field-label" for="llmKey">APIキー</label>
          <input id="llmKey" class="field" type="password" autocomplete="new-password" maxlength="2048">
          <p id="llmKeyStatus" class="inline-hint">空欄の場合は変更しません。</p>
          <label class="file-check"><input id="llmRemoveKey" type="checkbox"> 保存時に既存のキーを削除する</label>
        </div>
        <div id="llmNotice" class="notice" role="status" aria-live="polite"></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
          <button class="button" type="submit">設定を保存</button>
          <button id="llmSelect" class="button secondary" type="button">このバックエンドに切り替え</button>
        </div>
      </form>
    </article>`;

  panel.querySelector("#llmBackend").addEventListener("change", () => showBackend(panel));
  panel.querySelector("#llmRefresh").addEventListener("click", () => loadLlm(panel, ctx));

  panel.querySelector("#llmSelect").addEventListener("click", async () => {
    const backend = panel.querySelector("#llmBackend").value;
    if (!window.confirm(`${backend} に全体の会話・翻訳を切り替えますか？`)) return;
    try {
      llmStatus = await (await ctx.api("/api/llm/active", { method: "POST", body: JSON.stringify({ backend }) })).json();
      panel.querySelector("#llmActive").textContent = `現在使用中: ${llmStatus.backend}`;
      ctx.notice("#llmNotice", "バックエンドを切り替えました。", "success");
    } catch (error) {
      ctx.notice("#llmNotice", error.message);
    }
  });

  panel.querySelector("#llmForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      backend: panel.querySelector("#llmBackend").value,
      model: panel.querySelector("#llmModel").value.trim(),
      translate_model: panel.querySelector("#llmTranslate").value.trim(),
    };
    const url = panel.querySelector("#llmUrl").value.trim();
    if (url && url !== llmStatus?.backends[payload.backend]?.api_url) payload.api_url = url;
    const keyVal = panel.querySelector("#llmKey").value;
    const removeKey = panel.querySelector("#llmRemoveKey").checked;
    if (keyVal && removeKey) {
      ctx.notice("#llmNotice", "キーの入力と削除は同時に指定できません。");
      return;
    }
    if (keyVal) payload.api_key = keyVal;
    if (removeKey) payload.remove_api_key = true;
    try {
      llmStatus = await (await ctx.api("/api/llm/settings", { method: "PUT", body: JSON.stringify(payload) })).json();
      panel.querySelector("#llmBackend").value = payload.backend;
      showBackend(panel);
      ctx.notice("#llmNotice", "設定を保存しました。", "success");
    } catch (error) {
      ctx.notice("#llmNotice", error.message);
    } finally {
      panel.querySelector("#llmKey").value = "";
    }
  });
}

export async function activate(ctx) {
  await loadLlm(document.getElementById("llmPanel"), ctx);
}
