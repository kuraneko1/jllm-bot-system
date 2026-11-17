export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export const VOL_ICON_SVG =
  '<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">' +
  '<path fill="currentColor" d="M1.5 5.5v5h3L8.5 14V2L4.5 5.5h-3z"/>' +
  '<path d="M10.2 5.2a4 4 0 0 1 0 5.6M12 3.4a6.6 6.6 0 0 1 0 9.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';

const TABS = [
  "sound", "overview", "reading", "gif", "voice",
  "recorder", "playground", "requests", "listen",
];
const ADMIN_TABS = new Set(["voice", "requests"]);

const state = {
  authenticated: false,
  activeTab: "sound",
  assetV: document.body.dataset.assetV || "",
  editing: null,
  onEdited: null,
};

const loadedModules = new Map();

export function debounce(fn, wait = 150) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function notice(target, message, kind = "error") {
  const el = typeof target === "string" ? $(target) : target;
  if (!el) return;
  el.textContent = message;
  el.className = `notice visible ${kind}`;
}

export function clearNotice(target) {
  const el = typeof target === "string" ? $(target) : target;
  if (!el) return;
  el.textContent = "";
  el.className = "notice";
}

export function makeButton(label, className, callback) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className} small`;
  button.textContent = label;
  button.addEventListener("click", callback);
  return button;
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: options.body instanceof FormData
      ? (options.headers || {})
      : { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `リクエストに失敗しました（${response.status}）`;
    try {
      message = (await response.json()).error || message;
    } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    if (response.status === 401 && state.authenticated) {
      setScreen(false);
    }
    throw error;
  }
  return response;
}

// ── Audio volume & lightweight lazy player (no eager fetch/decode) ──
let sharedAudioCtx = null;

export function previewVolume() {
  try {
    const stored = localStorage.getItem("readingPreviewVolume");
    if (stored === null || stored === "") return 1;
    const raw = Number(stored);
    return Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 1;
  } catch (_) {
    return 1;
  }
}

export function savePreviewVolume(value) {
  try {
    localStorage.setItem("readingPreviewVolume", String(value));
  } catch (_) {}
}

export function setAudioVolume(audio, value) {
  const numeric = value === null || value === undefined || value === "" ? 1 : Number(value);
  const level = Number.isFinite(numeric) ? Math.min(3, Math.max(0, numeric)) : 1;
  if (level <= 1) {
    audio.volume = level;
    if (audio._gainNode) audio._gainNode.gain.value = 1;
    return;
  }
  audio.volume = 1;
  try {
    if (!audio._gainNode) {
      if (!sharedAudioCtx) sharedAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = sharedAudioCtx.createMediaElementSource(audio);
      const gain = sharedAudioCtx.createGain();
      source.connect(gain);
      gain.connect(sharedAudioCtx.destination);
      audio._gainNode = gain;
    }
    if (sharedAudioCtx.state === "suspended") sharedAudioCtx.resume().catch(() => {});
    audio._gainNode.gain.value = level;
  } catch (_) {}
}

export function applyPreviewVolume(audio) {
  setAudioVolume(audio, previewVolume());
}

function readStore(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "{}");
  } catch (_) {
    return {};
  }
}

export function getFileVolume(phrase, filename, fallback) {
  const val = readStore("soundFileVolumes")[`${phrase}\uFFFD${filename}`];
  return (typeof val === "number" && Number.isFinite(val)) ? Math.min(3, Math.max(0, val)) : fallback;
}

export function setFileVolume(phrase, filename, value) {
  const map = readStore("soundFileVolumes");
  map[`${phrase}\uFFFD${filename}`] = Math.min(3, Math.max(0, value));
  try { localStorage.setItem("soundFileVolumes", JSON.stringify(map)); } catch (_) {}
}

export function getVoiceSettings(name) {
  const item = readStore("voiceTuneSettings")[name] || {};
  return {
    speed: (typeof item.speed === "number" && Number.isFinite(item.speed)) ? Math.min(2, Math.max(0.5, item.speed)) : 1.0,
    volume: (typeof item.volume === "number" && Number.isFinite(item.volume)) ? Math.min(3, Math.max(0, item.volume)) : 1.0,
  };
}

export function setVoiceSettings(name, patch) {
  const map = readStore("voiceTuneSettings");
  map[name] = { ...getVoiceSettings(name), ...patch };
  try { localStorage.setItem("voiceTuneSettings", JSON.stringify(map)); } catch (_) {}
}

export function volumeSlider(audio, label, initial = previewVolume()) {
  const wrap = document.createElement("label");
  wrap.className = "vol-wrap";
  wrap.title = label;
  wrap.innerHTML = VOL_ICON_SVG;
  const input = document.createElement("input");
  input.type = "range";
  input.min = "0";
  input.max = "300";
  input.value = String(Math.round(initial * 100));
  input.setAttribute("aria-label", label);
  setAudioVolume(audio, initial);
  input.addEventListener("input", () => setAudioVolume(audio, Number(input.value) / 100));
  wrap.append(input);
  return wrap;
}

const fmtSec = (sec) => {
  const t = Number.isFinite(sec) ? Math.max(0, sec) : 0;
  return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
};

const WAVE_BARS = 24;

function waveformHeights(seedText) {
  let hash = 2166136261;
  for (let i = 0; i < seedText.length; i += 1) {
    hash ^= seedText.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const bars = [];
  for (let i = 0; i < WAVE_BARS; i += 1) {
    hash ^= hash << 13;
    hash ^= hash >>> 17;
    hash ^= hash << 5;
    const envelope = Math.sin(((i + 0.5) / WAVE_BARS) * Math.PI);
    const jitter = (hash >>> 0) / 4294967295;
    const pct = Math.round(22 + (0.35 + 0.65 * envelope) * (25 + jitter * 48));
    bars.push(Math.min(96, Math.max(20, pct)));
  }
  return bars;
}

export function createMiniPlayer(src, label, initialVolume = 1.0) {
  const wrap = document.createElement("div");
  wrap.className = "aplayer";

  const audio = document.createElement("audio");
  audio.preload = "none";
  audio.src = src;
  audio.setAttribute("aria-label", label);
  setAudioVolume(audio, initialVolume);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ap-btn";
  btn.setAttribute("aria-label", `${label}を再生`);
  btn.innerHTML =
    '<svg class="ap-ico-play" width="12" height="12" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 1.5 12 7 3 12.5Z" fill="currentColor"/></svg>' +
    '<svg class="ap-ico-pause" width="12" height="12" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 2v10M10 2v10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>';

  const wave = document.createElement("div");
  wave.className = "ap-wave";

  const barsWrap = document.createElement("div");
  barsWrap.className = "ap-wave-bars";
  barsWrap.setAttribute("aria-hidden", "true");
  const bars = waveformHeights(src).map((pct, idx) => {
    const bar = document.createElement("span");
    bar.className = "ap-bar";
    bar.style.height = `${pct}%`;
    bar.style.setProperty("--bar-delay", `${(idx % 6) * 90}ms`);
    barsWrap.append(bar);
    return bar;
  });

  const scrub = document.createElement("input");
  scrub.type = "range";
  scrub.className = "ap-scrub";
  scrub.min = "0";
  scrub.max = "1000";
  scrub.value = "0";
  scrub.setAttribute("aria-label", `${label}の再生位置`);
  wave.append(barsWrap, scrub);

  const time = document.createElement("span");
  time.className = "ap-time";
  time.textContent = "0:00 / --:--";

  const err = document.createElement("span");
  err.className = "ap-error";
  err.hidden = true;

  const paint = () => {
    const d = Number.isFinite(audio.duration) ? audio.duration : 0;
    const c = audio.currentTime || 0;
    const ratio = d > 0 ? Math.min(1, Math.max(0, c / d)) : 0;
    scrub.value = String(Math.round(ratio * 1000));
    time.textContent = `${fmtSec(c)} / ${d > 0 ? fmtSec(d) : "--:--"}`;
    const activeCount = ratio > 0 ? Math.max(1, Math.round(ratio * WAVE_BARS)) : 0;
    bars.forEach((bar, i) => bar.classList.toggle("active", i < activeCount));
    const isPlaying = !audio.paused;
    wrap.classList.toggle("playing", isPlaying);
    btn.classList.toggle("playing", isPlaying);
    btn.setAttribute("aria-label", `${label}を${audio.paused ? "再生" : "一時停止"}`);
  };

  btn.addEventListener("click", async () => {
    err.hidden = true;
    if (!audio.paused) {
      audio.pause();
      return;
    }
    $$("audio").forEach((other) => { if (other !== audio) other.pause(); });
    try {
      if (sharedAudioCtx?.state === "suspended") await sharedAudioCtx.resume();
      await audio.play();
    } catch (_) {
      err.textContent = "再生できませんでした";
      err.hidden = false;
    }
  });

  scrub.addEventListener("input", () => {
    const d = Number.isFinite(audio.duration) ? audio.duration : 0;
    if (d > 0) audio.currentTime = (Number(scrub.value) / 1000) * d;
  });

  ["loadedmetadata", "timeupdate", "play", "pause", "ended"].forEach((ev) => audio.addEventListener(ev, paint));
  audio.addEventListener("error", () => {
    err.textContent = "読み込み失敗";
    err.hidden = false;
  });

  wrap.append(btn, wave, time, err, audio);
  return { root: wrap, audio };
}

// ── Passkeys & Auth ──
function fromBase64url(value) {
  const b64 = value.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)), (c) => c.charCodeAt(0));
}

function toBase64url(value) {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function passkeyOptions(options, registration) {
  options.challenge = fromBase64url(options.challenge);
  if (registration) options.user.id = fromBase64url(options.user.id);
  for (const key of (registration ? options.excludeCredentials : options.allowCredentials) || []) {
    key.id = fromBase64url(key.id);
  }
  return { publicKey: options };
}

function passkeyCredential(credential) {
  const response = { clientDataJSON: toBase64url(credential.response.clientDataJSON) };
  if (credential.response.attestationObject) {
    response.attestationObject = toBase64url(credential.response.attestationObject);
    response.transports = credential.response.getTransports ? credential.response.getTransports() : [];
  } else {
    response.authenticatorData = toBase64url(credential.response.authenticatorData);
    response.signature = toBase64url(credential.response.signature);
    response.userHandle = credential.response.userHandle ? toBase64url(credential.response.userHandle) : null;
  }
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    response,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

function requirePasskeySupport() {
  if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) {
    throw new Error("このブラウザーではパスキーを利用できません。HTTPS で開いてください。");
  }
}

function passkeyError(error) {
  if (error.name === "NotAllowedError") return "端末認証がキャンセルされたか期限切れになりました。";
  if (error.name === "InvalidStateError") return "この端末のパスキーは登録済みです。";
  return error.message;
}

export async function registerNewPasskey(nameInput, listTarget, noticeTarget) {
  requirePasskeySupport();
  const options = await (await api("/api/passkeys/register/options", { method: "POST", body: "{}" })).json();
  const credential = await navigator.credentials.create(passkeyOptions(options, true));
  if (!credential) throw new Error("パスキーの登録がキャンセルされました。");
  await api("/api/passkeys/register/verify", {
    method: "POST",
    body: JSON.stringify({
      name: (nameInput?.value || "").trim() || "パスキー",
      credential: passkeyCredential(credential),
    }),
  });
  await refreshPasskeys(listTarget, noticeTarget);
  notice(noticeTarget, "パスキーを登録しました。", "success");
}

export async function refreshPasskeys(listTarget, noticeTarget) {
  const list = typeof listTarget === "string" ? $(listTarget) : listTarget;
  if (!list || !state.authenticated) return;
  const data = await (await api("/api/passkeys")).json();
  list.replaceChildren();
  if (!data.credentials.length) {
    list.textContent = "パスキーはまだ登録されていません。";
    return;
  }
  for (const key of data.credentials) {
    const row = document.createElement("div");
    row.className = "file-check";
    const name = document.createElement("span");
    name.textContent = key.name;
    const remove = makeButton("削除", "danger", async () => {
      if (!window.confirm(`「${key.name}」のパスキーを削除しますか？`)) return;
      try {
        await api("/api/passkeys", { method: "DELETE", body: JSON.stringify({ id: key.id }) });
        await refreshPasskeys(list, noticeTarget);
        notice(noticeTarget, "パスキーを削除しました。", "success");
      } catch (error) {
        notice(noticeTarget, error.message);
      }
    });
    row.append(name, remove);
    list.append(row);
  }
}

// ── Summary & Screen state ──
export async function refreshSummary() {
  try {
    const data = await (await api("/api/summary")).json();
    $("#entryCount").textContent = String(data.readings ?? 0);
    $("#soundCount").textContent = String(data.sounds ?? 0);
    $("#gifCount").textContent = String(data.gifs ?? 0);
    $("#readingTabCount").textContent = `(${data.readings ?? 0})`;
    $("#soundTabCount").textContent = `(${data.sounds ?? 0})`;
    $("#gifTabCount").textContent = `(${data.gifs ?? 0})`;
  } catch (_) {}
}

export function setScreen(isAdmin) {
  state.authenticated = Boolean(isAdmin);
  document.body.dataset.admin = String(state.authenticated);
  $("#dashboard").hidden = false;
  $("#loginButton").hidden = state.authenticated;
  $("#accessStatus").textContent = state.authenticated ? "管理モード" : "閲覧モード";
  if (!state.authenticated) {
    if ($("#entryEditor").open) $("#entryEditor").close();
    if (ADMIN_TABS.has(state.activeTab)) chooseTab("sound");
  }
}

// ── Shared Editor Dialog ──
export function openEditor(kind, entry, onSaved) {
  state.editing = { kind, original: entry.trigger || entry.phrase };
  state.onEdited = onSaved || null;
  const isReading = kind === "reading";
  $("#editorTitle").textContent = `${{ reading: "特殊読み", sound: "音声辞書", gif: "GIF辞書" }[kind]}を編集`;
  $("#editorDescription").textContent = isReading
    ? "登録語と読みを変更します。"
    : "登録語を変更します。音声ファイルの参照も調整できます。";
  $("#editorTrigger").value = state.editing.original;
  $("#editorTrigger").maxLength = kind === "gif" ? 80 : 100;
  $("#editorReadingGroup").hidden = !isReading;
  $("#editorReading").required = isReading;
  $("#editorReading").value = isReading ? entry.reading : "";
  $("#editorFilesGroup").hidden = kind !== "sound";
  $("#editorFiles").replaceChildren();
  if (kind === "sound") {
    for (const file of entry.files) {
      const label = document.createElement("label");
      label.className = "file-check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.value = file;
      label.append(checkbox, document.createTextNode(file));
      $("#editorFiles").append(label);
    }
  }
  clearNotice("#editorNotice");
  $("#entryEditor").showModal();
}

$("#editorCancel").addEventListener("click", () => $("#entryEditor").close());
$("#editorForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.editing) return;
  const { kind, original } = state.editing;
  const key = $("#editorTrigger").value.trim();
  const fields = kind === "reading"
    ? { original, trigger: key, reading: $("#editorReading").value.trim() }
    : kind === "sound"
      ? { original, phrase: key, files: $$("#editorFiles input:checked").map((n) => n.value) }
      : { original, trigger: key };
  try {
    await api(`/api/${{ reading: "readings", sound: "sounds", gif: "gifs" }[kind]}`, {
      method: "PUT",
      body: JSON.stringify(fields),
    });
    $("#entryEditor").close();
    if (state.onEdited) await state.onEdited();
    await refreshSummary();
    notice("#globalNotice", `「${key}」を更新しました。`, "success");
  } catch (error) {
    notice("#editorNotice", error.message);
  }
});

// ── Mobile Nav Drawer ──
const mobileNav = window.matchMedia("(max-width: 900px)");
const navTabs = $("nav.tabs");
const navActions = $("#topActions");
const navHome = [
  navTabs ? { node: navTabs, parent: navTabs.parentNode, next: navTabs.nextSibling } : null,
  navActions ? { node: navActions, parent: navActions.parentNode, next: navActions.nextSibling } : null,
].filter(Boolean);

function restoreNavHome() {
  for (const item of navHome) {
    if (item.node.parentNode !== item.parent) item.parent.insertBefore(item.node, item.next);
  }
}

function setNavOpen(open) {
  const next = Boolean(open) && mobileNav.matches && !$("#dashboard").hidden;
  document.body.classList.toggle("nav-open", next);
  $("#navToggle").setAttribute("aria-expanded", String(next));
}

function closeNav() {
  setNavOpen(false);
}

export function placeNavChrome() {
  restoreNavHome();
  if (!mobileNav.matches) {
    setNavOpen(false);
    return;
  }
  $("#drawerTabsSlot").append(navTabs);
  $("#drawerActionsSlot").append(navActions);
}

$("#navToggle").addEventListener("click", () => setNavOpen(!document.body.classList.contains("nav-open")));
$("#navClose").addEventListener("click", closeNav);
$("#navBackdrop").addEventListener("click", closeNav);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("nav-open")) closeNav();
});
mobileNav.addEventListener?.("change", placeNavChrome);

// ── Theme Toggle (Dark / Light) ──
const THEME_META_COLOR = { dark: "#101216", light: "#f3f5f7" };
const SUN_ICON =
  '<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' +
  '<circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
  '<path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
const MOON_ICON =
  '<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' +
  '<path d="M13.2 9.8A5.4 5.4 0 0 1 6.2 2.8 5.6 5.6 0 1 0 13.2 9.8Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme, persist = true) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  const meta = $("#themeColorMeta");
  if (meta) meta.setAttribute("content", THEME_META_COLOR[next]);
  const btn = $("#themeToggle");
  if (btn) {
    btn.innerHTML = next === "dark" ? `${SUN_ICON}<span>ライト</span>` : `${MOON_ICON}<span>ダーク</span>`;
    btn.setAttribute("aria-label", next === "dark" ? "ライトモードへ切り替え" : "ダークモードへ切り替え");
    btn.dataset.theme = next;
  }
  if (persist) {
    try { localStorage.setItem("jllmTheme", next); } catch (_) {}
  }
  window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: next } }));
}

$("#themeToggle")?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});
applyTheme(currentTheme(), false);

// ── Custom Dropdowns (replaces native <select> chrome) ──
let openSelectState = null;

function closeOpenSelect(restoreFocus = false) {
  if (!openSelectState) return;
  const { wrap, menu, trigger } = openSelectState;
  menu.hidden = true;
  if (menu.parentNode !== wrap) wrap.append(menu);
  trigger.setAttribute("aria-expanded", "false");
  wrap.classList.remove("open");
  if (restoreFocus) trigger.focus();
  openSelectState = null;
}

export function enhanceSelect(select) {
  if (!select || select.dataset.enhanced === "1") return;
  select.dataset.enhanced = "1";

  const wrap = document.createElement("div");
  wrap.className = "cselect";
  if (select.classList.contains("select")) wrap.classList.add("cselect-field");
  select.parentNode.insertBefore(wrap, select);
  select.classList.add("select-native");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "cselect-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const ariaLabel = select.getAttribute("aria-label");
  if (ariaLabel) trigger.setAttribute("aria-label", ariaLabel);

  const labelSpan = document.createElement("span");
  labelSpan.className = "cselect-label";
  const arrowSpan = document.createElement("span");
  arrowSpan.className = "cselect-arrow";
  arrowSpan.setAttribute("aria-hidden", "true");
  arrowSpan.innerHTML =
    '<svg viewBox="0 0 12 8" width="11" height="7" fill="none">' +
    '<path d="M1.5 1.75 6 6.25l4.5-4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  trigger.append(labelSpan, arrowSpan);

  const menu = document.createElement("div");
  menu.className = "cselect-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  wrap.append(trigger, select, menu);

  const sync = () => {
    const options = Array.from(select.options);
    const active = options.find((opt) => opt.value === select.value) || options[select.selectedIndex] || options[0];
    labelSpan.textContent = active ? active.textContent : "";
    trigger.disabled = select.disabled;
    menu.replaceChildren();
    for (const opt of options) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cselect-option";
      btn.setAttribute("role", "option");
      const isSelected = active ? opt === active : opt.selected;
      btn.setAttribute("aria-selected", String(isSelected));
      btn.textContent = opt.textContent;
      btn.disabled = opt.disabled;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (opt.disabled) return;
        const changed = select.value !== opt.value;
        select.value = opt.value;
        closeOpenSelect(true);
        if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      menu.append(btn);
    }
  };

  const protoDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  if (protoDesc?.get && protoDesc?.set) {
    Object.defineProperty(select, "value", {
      configurable: true,
      get() { return protoDesc.get.call(this); },
      set(val) {
        protoDesc.set.call(this, val);
        sync();
      },
    });
  }

  const observer = new MutationObserver(sync);
  observer.observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "label"] });
  select.addEventListener("change", sync);
  sync();

  const openMenu = () => {
    if (trigger.disabled) return;
    sync();
    if (openSelectState && openSelectState.wrap === wrap) {
      closeOpenSelect();
      return;
    }
    closeOpenSelect();
    document.body.append(menu);
    menu.hidden = false;
    wrap.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");

    const rect = trigger.getBoundingClientRect();
    const minW = Math.max(rect.width, 168);
    menu.style.minWidth = `${Math.round(minW)}px`;
    menu.style.maxWidth = `${Math.max(220, window.innerWidth - 16)}px`;
    const menuH = Math.min(menu.scrollHeight || 220, 280);
    const spaceBelow = window.innerHeight - rect.bottom - 8;
    const top = spaceBelow >= Math.min(menuH, 140) || spaceBelow >= rect.top - 8
      ? Math.min(rect.bottom + 4, window.innerHeight - menuH - 8)
      : Math.max(8, rect.top - menuH - 4);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8));
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(Math.max(8, top))}px`;

    const current = menu.querySelector('[aria-selected="true"]');
    current?.scrollIntoView({ block: "nearest" });
    openSelectState = { wrap, menu, trigger };
  };

  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openMenu();
  });

  trigger.addEventListener("keydown", (ev) => {
    const items = () => Array.from(menu.querySelectorAll(".cselect-option:not(:disabled)"));
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!openSelectState || openSelectState.wrap !== wrap) openMenu();
      const opts = items();
      if (!opts.length) return;
      const curIdx = opts.findIndex((o) => o === document.activeElement || o.getAttribute("aria-selected") === "true");
      const delta = ev.key === "ArrowDown" ? 1 : -1;
      const next = opts[(curIdx + delta + opts.length) % opts.length];
      next?.focus();
    } else if (ev.key === "Escape" && openSelectState?.wrap === wrap) {
      ev.preventDefault();
      closeOpenSelect(true);
    }
  });

  menu.addEventListener("keydown", (ev) => {
    const opts = Array.from(menu.querySelectorAll(".cselect-option:not(:disabled)"));
    const idx = opts.indexOf(document.activeElement);
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      opts[(idx + 1) % opts.length]?.focus();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      opts[(idx - 1 + opts.length) % opts.length]?.focus();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      closeOpenSelect(true);
    }
  });
}

export function enhanceSelects(root = document) {
  $$("select", root).forEach(enhanceSelect);
}

document.addEventListener("pointerdown", (ev) => {
  if (!openSelectState) return;
  if (!openSelectState.wrap.contains(ev.target) && !openSelectState.menu.contains(ev.target)) {
    closeOpenSelect();
  }
});
document.addEventListener("scroll", (ev) => {
  if (openSelectState && !openSelectState.menu.contains(ev.target)) closeOpenSelect();
}, true);
window.addEventListener("resize", () => closeOpenSelect());

// ── Lazy Tab Router ──
const ctx = {
  $,
  $$,
  api,
  notice,
  clearNotice,
  debounce,
  makeButton,
  openEditor,
  refreshSummary,
  refreshPasskeys,
  registerNewPasskey,
  createMiniPlayer,
  volumeSlider,
  applyPreviewVolume,
  setAudioVolume,
  previewVolume,
  savePreviewVolume,
  getFileVolume,
  setFileVolume,
  getVoiceSettings,
  setVoiceSettings,
  placeNavChrome,
  chooseTab,
  enhanceSelects,
  isAuthenticated: () => state.authenticated,
  passkeyError,
};

const MODULE_VERSION_SUFFIX = state.assetV ? `?v=${encodeURIComponent(state.assetV)}` : "";

const TAB_MODULES = {
  sound: "sounds.js",
  overview: "overview.js",
  reading: "readings.js",
  gif: "gifs.js",
  voice: "voices.js",
  recorder: "recorder.js",
  playground: "playground.js",
  requests: "requests.js",
  listen: "listen.js",
};

async function loadTabModule(name) {
  if (loadedModules.has(name)) return loadedModules.get(name);
  const file = TAB_MODULES[name] || `${name}.js`;
  const mod = await import(`/static/tabs/${file}${MODULE_VERSION_SUFFIX}`);
  loadedModules.set(name, mod);
  return mod;
}

export async function chooseTab(name) {
  if (!TABS.includes(name)) return;
  if (ADMIN_TABS.has(name) && !state.authenticated) return;

  const prevName = state.activeTab;
  if (prevName && prevName !== name) {
    const prevMod = loadedModules.get(prevName);
    prevMod?.deactivate?.(ctx);
  }
  state.activeTab = name;

  for (const tab of TABS) {
    const selected = tab === name;
    const panel = $(`#${tab}Panel`);
    const button = $(`#${tab}Tab`);
    if (panel) panel.hidden = !selected;
    if (button) {
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    }
  }

  clearNotice("#globalNotice");

  const panel = $(`#${name}Panel`);
  try {
    const mod = await loadTabModule(name);
    if (panel && !panel.dataset.mounted && mod.mount) {
      await mod.mount(panel, ctx);
      panel.dataset.mounted = "1";
      enhanceSelects(panel);
    }
    if (state.activeTab === name && mod.activate) {
      await mod.activate(ctx);
      enhanceSelects(panel);
    }
  } catch (error) {
    notice("#globalNotice", error.message || "画面を読み込めませんでした。");
  }

  placeNavChrome();
  closeNav();
}

$$(".tab[data-tab]").forEach((tab) => {
  tab.addEventListener("click", () => chooseTab(tab.dataset.tab));
});

// ── Viewer link ──
const htmlLink = $("#htmlDashboardLink");
if (htmlLink) {
  const url = String(htmlLink.dataset.url || "").trim();
  if (/^https?:\/\//.test(url)) {
    htmlLink.href = url.replace(/\/+$/, "") + "/";
    htmlLink.hidden = false;
  }
}

// ── Login / Logout ──
$("#passkeyLogin").addEventListener("click", async () => {
  const button = $("#passkeyLogin");
  button.disabled = true;
  clearNotice("#loginNotice");
  try {
    requirePasskeySupport();
    const options = await (await api("/api/passkeys/login/options", { method: "POST", body: "{}" })).json();
    const credential = await navigator.credentials.get(passkeyOptions(options, false));
    if (!credential) throw new Error("ログインがキャンセルされました。");
    await api("/api/passkeys/login/verify", {
      method: "POST",
      body: JSON.stringify({ credential: passkeyCredential(credential) }),
    });
    $("#loginScreen").close();
    setScreen(true);
    await refreshSummary();
    await chooseTab(state.activeTab);
  } catch (error) {
    notice("#loginNotice", passkeyError(error));
  } finally {
    button.disabled = false;
  }
});

$("#passkeyFirstRegister").addEventListener("click", async () => {
  const button = $("#passkeyFirstRegister");
  button.disabled = true;
  clearNotice("#loginNotice");
  try {
    requirePasskeySupport();
    const options = await (await api("/api/passkeys/register/options", { method: "POST", body: "{}" })).json();
    const credential = await navigator.credentials.create(passkeyOptions(options, true));
    if (!credential) throw new Error("パスキーの登録がキャンセルされました。");
    await api("/api/passkeys/register/verify", {
      method: "POST",
      body: JSON.stringify({ name: "管理者のパスキー", credential: passkeyCredential(credential) }),
    });
    notice("#loginNotice", "パスキーを登録しました。続けてログインします。", "success");
    $("#passkeyFirstRegister").hidden = true;
    $("#passkeyLogin").click();
  } catch (error) {
    notice("#loginNotice", passkeyError(error));
  } finally {
    button.disabled = false;
  }
});

$("#loginButton").addEventListener("click", async () => {
  clearNotice("#loginNotice");
  try {
    const session = await (await fetch("/api/session", { credentials: "same-origin" })).json();
    $("#passkeyFirstRegister").hidden = Boolean(session.passkey_registered);
  } catch (_) {
    $("#passkeyFirstRegister").hidden = true;
  }
  $("#loginScreen").showModal();
});

$("#loginCancel").addEventListener("click", () => $("#loginScreen").close());

$("#logoutButton").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_) {}
  setScreen(false);
  await chooseTab(state.activeTab);
});

// ── Boot ──
(async () => {
  placeNavChrome();
  try {
    const session = await (await fetch("/api/session", { credentials: "same-origin" })).json();
    setScreen(Boolean(session.authenticated));
  } catch (_) {
    setScreen(false);
  }
  refreshSummary();
  const wantedTab = new URLSearchParams(location.search).get("tab");
  const initialTab = TABS.includes(wantedTab) && (!ADMIN_TABS.has(wantedTab) || state.authenticated)
    ? wantedTab
    : "sound";
  await chooseTab(initialTab);
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
