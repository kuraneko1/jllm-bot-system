# ruff: noqa: E501

import asyncio
import base64
import html as html_lib
import json
import logging
import mimetypes
import re
import shutil
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")


def date_label(record: dict) -> str:
    """登録日を JST の YYYY/MM/DD で出す。読めない値はそのまま短く返す。"""

    raw = str(record.get("created_at", ""))
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:10]
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(JST).strftime("%Y/%m/%d")


GLYPHS = (
    '<path d="M24 9 40 37H8Z"/><path d="M24 21v7"/>',
    '<circle cx="24" cy="24" r="6"/>'
    '<ellipse cx="24" cy="24" rx="16" ry="7" transform="rotate(-24 24 24)"/>',
    '<rect x="10" y="10" width="20" height="20" rx="5"/>'
    '<rect x="21" y="21" width="17" height="17" rx="5"/>',
    '<path d="M8 30c6-15 10-15 16 0s10 15 16 0"/>',
    '<circle cx="24" cy="24" r="13"/><path d="M11 24h26M24 11v26"/>',
    '<path d="M12 36V22a12 12 0 0 1 24 0v14"/><path d="M12 36h24"/>',
)

_THUMB_LOCK = threading.Lock()

CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


def _hash32(value: str) -> int:
    h = 2166136261
    for ch in value:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _hue(value: str) -> int:
    return _hash32(value) % 360


def _badge(site_id: str) -> str:
    """絵文字を使わない SVG アイコン（サムネイルが無いページ用）。"""

    h = _hash32(site_id or "html")
    hue = h % 360
    glyph = GLYPHS[(h >> 8) % len(GLYPHS)]
    return (
        f'<span class="badge" style="--h:{hue}">'
        '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3.1" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        f"{glyph}</svg></span>"
    )


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _og_meta(
    *,
    title: str,
    description: str,
    url: str = "",
    image_url: str = "",
    author: str = "",
    site_name: str = "HTML Dashboard",
) -> str:
    """Open Graph / Twitter Card のリンクプレビュー用メタタグを組み立てる。"""

    tags = [
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{html_lib.escape(site_name, quote=True)}">',
        f'<meta property="og:title" content="{html_lib.escape(title, quote=True)}">',
        f'<meta property="og:description" content="{html_lib.escape(description, quote=True)}">',
    ]
    if url:
        tags.append(f'<meta property="og:url" content="{html_lib.escape(url, quote=True)}">')
    if image_url:
        safe_img = html_lib.escape(image_url, quote=True)
        tags.extend(
            [
                f'<meta property="og:image" content="{safe_img}">',
                '<meta property="og:image:width" content="960">',
                '<meta property="og:image:height" content="600">',
                '<meta name="twitter:card" content="summary_large_image">',
                f'<meta name="twitter:image" content="{safe_img}">',
            ]
        )
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    tags.extend(
        [
            f'<meta name="twitter:title" content="{html_lib.escape(title, quote=True)}">',
            f'<meta name="twitter:description" content="{html_lib.escape(description, quote=True)}">',
            f'<meta name="description" content="{html_lib.escape(description, quote=True)}">',
        ]
    )
    if author:
        tags.append(f'<meta name="author" content="{html_lib.escape(author, quote=True)}">')
    return "\n".join(tags)


_THUMB_READY_JS = """(() => {
  const ready = document.readyState === "complete";
  const vw = window.innerWidth || 960;
  const vh = window.innerHeight || 600;
  const sels = [
    "#loader", "#load", "#loading",
    "[id*='loader']", "[id*='loading']",
    ".loader", ".loading",
    "[class*='loader']", "[class*='loading']"
  ];
  let blocking = false;
  for (const el of document.querySelectorAll(sels.join(","))) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity || "1") <= 0.1) {
      continue;
    }
    const r = el.getBoundingClientRect();
    if (r.width >= vw * 0.45 && r.height >= vh * 0.45) {
      blocking = true;
      break;
    }
  }
  return { ready, blocking };
})()"""


def _resolve_thumb_file(root_resolved: Path, rel: str, entry_name: str) -> tuple[int, Path | None, str]:
    target = (root_resolved / (rel or entry_name)).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return 403, None, ""
    if not target.is_file():
        return 404, None, ""
    ctype, _ = mimetypes.guess_type(target.name)
    return 200, target, ctype or "application/octet-stream"


async def _start_thumb_server(root: Path, entry_name: str) -> tuple[web.AppRunner, str]:
    """file:// の CDN 読み込み停滞を避けるため、ローカルループバック HTTP で配信する。"""

    root_resolved = await asyncio.to_thread(root.resolve)

    async def handle(request: web.Request) -> web.StreamResponse:
        rel = request.match_info.get("path", "")
        status, target, ctype = await asyncio.to_thread(
            _resolve_thumb_file, root_resolved, rel, entry_name
        )
        if status != 200 or target is None:
            return web.Response(status=status)
        return web.FileResponse(target, headers={"Content-Type": ctype})

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/{path:.*}", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}/{entry_name}"


async def _wait_page_ready(command, wait: float) -> None:
    """ページ読込完了と全画面ローダーの消滅を待ち、描画安定後に戻る。"""

    loop = asyncio.get_running_loop()
    start = loop.time()
    min_wait = min(wait, 2.0)
    max_wait = max(wait, 25.0)
    settle = min(1.2, max(0.1, wait * 0.2))
    ready_since: float | None = None

    while True:
        elapsed = loop.time() - start
        if elapsed >= max_wait:
            return
        state = None
        try:
            res = await command(
                "Runtime.evaluate",
                {"expression": _THUMB_READY_JS, "returnByValue": True},
            )
            state = res.get("result", {}).get("value")
        except Exception:
            state = None
        if isinstance(state, dict) and state.get("ready") and not state.get("blocking"):
            if ready_since is None:
                ready_since = loop.time()
            if elapsed >= min_wait and (loop.time() - ready_since) >= settle:
                return
        else:
            ready_since = None
        await asyncio.sleep(0.35)


async def capture_thumbnail(
    chrome: str,
    source: Path,
    out: Path,
    width: int = 960,
    height: int = 600,
    wait: float = 7.0,
) -> bool:
    """1ページを一度だけヘッドレスで描画して JPEG サムネイルにする。"""

    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp が無いためサムネイルを生成できません")
        return False

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = out.parent / f".thumb-profile-{uuid.uuid4().hex[:8]}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--mute-audio",
        "--enable-unsafe-swiftshader",
        f"--window-size={width},{height}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    payload = None
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            target = None
            for _ in range(60):
                try:
                    async with session.get(f"http://127.0.0.1:{port}/json/list") as response:
                        targets = await response.json(content_type=None)
                except Exception:
                    targets = []
                pages = [item for item in targets if item.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
                await asyncio.sleep(0.2)
            if target is None:
                logger.warning("Chrome のデバッグ対象が見つかりません: %s", source)
                return False
            ws = await session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0)
            counter = 0

            async def command(method, params=None):
                nonlocal counter
                counter += 1
                await ws.send_json({"id": counter, "method": method, "params": params or {}})
                while True:
                    message = await ws.receive()
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        raise RuntimeError(f"CDP 接続が切れました: {message.type}")
                    data = json.loads(message.data)
                    if data.get("id") != counter:
                        continue
                    if "error" in data:
                        raise RuntimeError(str(data["error"]))
                    return data.get("result", {})

            await command("Page.enable")
            await command("Runtime.enable")
            await command(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
            thumb_runner, thumb_url = await _start_thumb_server(source.parent, source.name)
            try:
                await command("Page.navigate", {"url": thumb_url})
                await _wait_page_ready(command, wait)
                shot = await command("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
            finally:
                await thumb_runner.cleanup()
            payload = base64.b64decode(shot["data"])
            await ws.close()
    except Exception:
        logger.exception("サムネイル生成に失敗: %s", source)
        return False
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    if not payload:
        return False
    await asyncio.to_thread(out.write_bytes, payload)
    return True

DASHBOARD_CSS = """
*{box-sizing:border-box}
[hidden]{display:none!important}
:root{
  color-scheme:dark;
  --bg:#101216;--surface:#171a20;--surface-2:#1d2129;--sunken:#12141a;
  --line:rgba(255,255,255,.085);--line-2:rgba(255,255,255,.17);
  --text:#f4f4f5;--ink-2:#d4d4d8;--muted:#9ca3af;
  --accent:#f59e0b;--accent-2:#fbbf24;--accent-ink:#171103;
  --accent-soft:rgba(245,158,11,.13);--green:#22c55e;
  --scroll-track:#101216;--scroll-thumb:rgba(255,255,255,.22);
  --shadow:0 12px 30px rgba(0,0,0,.45);
  --r-sm:4px;--r-md:6px;--r-lg:8px;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f3f5f7;--surface:#ffffff;--surface-2:#f8fafc;--sunken:#eef2f6;
  --line:rgba(15,23,42,.09);--line-2:rgba(15,23,42,.18);
  --text:#111827;--ink-2:#374151;--muted:#64748b;
  --accent:#d97706;--accent-2:#b45309;--accent-ink:#ffffff;
  --accent-soft:rgba(217,119,6,.11);--green:#16a34a;
  --scroll-track:#eef2f6;--scroll-thumb:rgba(15,23,42,.22);
  --shadow:0 12px 30px rgba(15,23,42,.12);
}
html{scrollbar-width:thin;scrollbar-color:var(--scroll-thumb) var(--scroll-track)}
*::-webkit-scrollbar{width:9px;height:9px}
*::-webkit-scrollbar-track{background:var(--scroll-track)}
*::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border-radius:5px;border:2px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:var(--accent);border:2px solid transparent;background-clip:content-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);
  font:13.5px/1.6 ui-sans-serif,system-ui,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
  -webkit-font-smoothing:antialiased}
button,input{font:inherit;color:inherit}
button{cursor:pointer}
a{color:inherit}
:where(button,input,a,[tabindex]):focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1340px;margin:0 auto;padding:18px 20px 64px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  padding-bottom:14px;border-bottom:1px solid var(--line)}
.brand-row{display:flex;align-items:center;gap:10px;min-width:0}
.mark{width:30px;height:30px;border-radius:var(--r-sm);display:grid;place-items:center;flex:none;
  color:var(--accent);background:var(--accent-soft);border:1px solid rgba(245,158,11,.35)}
.mark svg{width:17px;height:17px}
.brand-text{display:flex;flex-direction:column;min-width:0}
.kicker{color:var(--accent-2);font-size:10.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
h1{font-size:19px;margin:0;font-weight:800;letter-spacing:-.02em;line-height:1.25}
.top-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.stat-pill{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;border-radius:var(--r-sm);
  border:1px solid var(--line);background:var(--surface);font-size:12px;color:var(--muted)}
.stat-pill b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text);font-variant-numeric:tabular-nums}
.stat-dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.btn{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 11px;
  border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--surface);
  color:var(--ink-2);text-decoration:none;font-size:12px;font-weight:600;transition:border-color .14s,color .14s,background .14s}
.btn:hover{border-color:var(--accent);color:var(--accent-2);background:var(--surface-2)}
.btn[aria-pressed="true"]{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-2)}
.btn svg{width:14px;height:14px;flex:none}
.backlink{display:inline-flex;align-items:center;gap:6px;color:var(--muted);text-decoration:none;
  font-size:12px;font-weight:600;margin-bottom:10px;padding:4px 8px 4px 5px;border-radius:var(--r-sm);
  border:1px solid var(--line);background:var(--surface)}
.backlink:hover{color:var(--text);border-color:var(--accent)}
.backlink svg{width:14px;height:14px}

.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
  margin-top:16px;padding:12px 14px;border-radius:var(--r-lg);border:1px solid var(--line);background:var(--surface)}
.search-box{position:relative;flex:1 1 240px;min-width:180px}
.search-box svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:14px;height:14px;color:var(--muted);pointer-events:none}
.search-input{width:100%;min-height:36px;padding:6px 10px 6px 32px;border-radius:var(--r-sm);
  border:1px solid var(--line-2);background:var(--sunken);color:var(--text);font-size:13px}
.search-input:focus{outline:none;border-color:var(--accent)}
.controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cselect{position:relative;display:inline-block;max-width:100%}
.cselect-btn{display:inline-flex;align-items:center;justify-content:space-between;gap:8px;min-width:148px;max-width:100%;
  min-height:36px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--line-2);
  background:var(--sunken);color:var(--text);font-size:12.5px;font-weight:600}
.cselect-btn:hover,.cselect.open .cselect-btn{border-color:var(--accent)}
.cselect-btn svg{width:12px;height:12px;color:var(--muted);transition:transform .14s}
.cselect.open .cselect-btn svg{transform:rotate(180deg);color:var(--accent-2)}
.cselect-menu{position:absolute;left:0;top:calc(100% + 4px);z-index:60;min-width:100%;padding:4px;
  border-radius:var(--r-md);border:1px solid var(--line-2);background:var(--surface);box-shadow:var(--shadow);
  display:flex;flex-direction:column;gap:2px}
.cselect-opt{width:100%;text-align:left;padding:7px 10px;border:0;border-radius:var(--r-sm);
  background:transparent;color:var(--ink-2);font-size:12.5px;font-weight:500}
.cselect-opt:hover{background:var(--surface-2);color:var(--text)}
.cselect-opt[aria-selected="true"]{background:var(--accent-soft);color:var(--accent-2);font-weight:700}

.view-seg{display:inline-flex;padding:2px;border-radius:var(--r-sm);border:1px solid var(--line);background:var(--sunken)}
.view-seg button{min-height:30px;padding:0 9px;border:0;border-radius:3px;background:transparent;color:var(--muted);
  display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600}
.view-seg button[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent-2)}
.view-seg svg{width:14px;height:14px}

.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;border-radius:var(--r-sm);
  border:1px solid var(--line);background:var(--surface);color:var(--muted);font-size:12px;font-weight:600;transition:.14s}
.chip:hover{color:var(--text);border-color:var(--line-2)}
.chip[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent-2);border-color:var(--accent)}
.chip .n{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;opacity:.78}
.chip .avatar{width:14px;height:14px;border-radius:3px;background:hsl(var(--h) 62% 52%)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:12px;margin-top:14px}
.grid.is-list{grid-template-columns:1fr;gap:8px}
.card{display:flex;flex-direction:column;border-radius:var(--r-lg);overflow:hidden;border:1px solid var(--line);
  background:var(--surface);text-decoration:none;color:inherit;transition:border-color .15s,transform .15s,box-shadow .15s}
.card:hover{transform:translateY(-2px);border-color:var(--accent);box-shadow:var(--shadow)}
.thumb{position:relative;display:grid;place-items:center;aspect-ratio:16/10;overflow:hidden;
  background:linear-gradient(145deg,hsl(var(--h) 38% 22%),hsl(calc(var(--h) + 32) 34% 13%));border-bottom:1px solid var(--line)}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:top center;
  opacity:0;transition:opacity .3s ease,transform .25s ease;display:block}
.card:hover .thumb img{transform:scale(1.02)}
.thumb.has-thumb img{opacity:1}
.thumb .badge{width:50px;height:50px;border-radius:var(--r-md);display:grid;place-items:center;
  background:rgba(16,18,22,.55);color:hsl(var(--h) 85% 70%);border:1px solid rgba(255,255,255,.16)}
.thumb .badge svg{width:26px;height:26px}
.thumb.has-thumb .badge{display:none}
.card-body{padding:11px 13px 12px;display:flex;flex-direction:column;gap:6px;min-width:0}
.card h3{margin:0;font-size:13.5px;font-weight:700;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .meta{display:flex;align-items:center;gap:8px;min-width:0}
.card .owner{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px;
  min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .date{margin-left:auto;flex:0 0 auto;color:var(--muted);font-size:11.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.card .owner .avatar{width:14px;height:14px;border-radius:3px;flex:0 0 auto;
  background:linear-gradient(140deg,hsl(var(--h) 65% 56%),hsl(calc(var(--h) + 35) 60% 42%))}

.grid.is-list .card{flex-direction:row;align-items:center}
.grid.is-list .thumb{width:136px;flex:0 0 auto;aspect-ratio:16/10;border-bottom:0;border-right:1px solid var(--line)}
.grid.is-list .card-body{flex:1;padding:10px 14px}

.empty{color:var(--muted);padding:36px 16px;text-align:center;border:1px dashed var(--line);border-radius:var(--r-lg);grid-column:1/-1}
footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);
  font-size:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:space-between}
footer a{display:inline-flex;align-items:center;gap:6px;color:var(--ink-2);text-decoration:none;
  padding:5px 10px;border-radius:var(--r-sm);border:1px solid var(--line);background:var(--surface)}
footer a:hover{color:var(--accent-2);border-color:var(--accent)}
footer a svg{width:14px;height:14px}
.shown-count{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}

dialog.viewer-modal{width:min(1180px,calc(100vw - 24px));height:min(86vh,860px);padding:0;
  border:1px solid var(--line-2);border-radius:var(--r-lg);background:var(--surface);color:var(--text);box-shadow:var(--shadow)}
dialog.viewer-modal::backdrop{background:rgba(0,0,0,.72)}
.modal-shell{display:flex;flex-direction:column;width:100%;height:100%}
.modal-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 14px;
  border-bottom:1px solid var(--line);background:var(--surface-2)}
.modal-title{font-weight:700;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-actions{display:flex;align-items:center;gap:8px;flex:none}
.modal-frame{flex:1;width:100%;border:0;background:#fff}

@media (max-width:900px),(pointer:coarse){
  .search-input{font-size:16px}
}
@media (max-width:640px){
  .wrap{padding:14px 12px 48px}
  .grid.is-list .thumb{width:96px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

DASHBOARD_JS = r"""
(() => {
  const root = document.documentElement;
  const themeBtn = document.getElementById('themeBtn');
  const grid = document.getElementById('grid');
  const chips = document.getElementById('chips');
  const cards = [...document.querySelectorAll('.card')];
  const counter = document.getElementById('shown');
  const searchInput = document.getElementById('searchInput');
  const sortWrap = document.getElementById('sortSelect');
  const sortBtn = document.getElementById('sortBtn');
  const sortLabel = document.getElementById('sortLabel');
  const sortMenu = document.getElementById('sortMenu');
  const viewBtns = [...document.querySelectorAll('[data-view-mode]')];
  const previewToggle = document.getElementById('previewToggle');
  const modal = document.getElementById('viewerModal');
  const modalTitle = document.getElementById('modalTitle');
  const modalOpenTab = document.getElementById('modalOpenTab');
  const modalFrame = document.getElementById('modalFrame');
  const modalClose = document.getElementById('modalClose');

  let activeOwner = 'all';
  let activeSort = 'newest';
  let query = '';
  let inlinePreview = false;

  function applyTheme(next, save) {
    const t = next === 'light' ? 'light' : 'dark';
    root.dataset.theme = t;
    if (themeBtn) {
      themeBtn.setAttribute('aria-label', t === 'dark' ? 'ライトモードへ切替' : 'ダークモードへ切替');
      const label = themeBtn.querySelector('span');
      if (label) label.textContent = t === 'dark' ? 'ライト' : 'ダーク';
    }
    if (save) { try { localStorage.setItem('jllmHtmlTheme', t); } catch (_) {} }
  }
  try {
    applyTheme(localStorage.getItem('jllmHtmlTheme') === 'light' ? 'light' : 'dark', false);
  } catch (_) {
    applyTheme('dark', false);
  }
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      applyTheme(root.dataset.theme === 'light' ? 'dark' : 'light', true);
    });
  }

  function applyView(mode) {
    const listMode = mode === 'list';
    if (grid) grid.classList.toggle('is-list', listMode);
    viewBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.viewMode === mode)));
    try { localStorage.setItem('jllmHtmlView', mode); } catch (_) {}
  }
  try {
    const savedView = localStorage.getItem('jllmHtmlView');
    if (savedView === 'list' || savedView === 'grid') applyView(savedView);
  } catch (_) {}
  viewBtns.forEach((b) => b.addEventListener('click', () => applyView(b.dataset.viewMode)));

  function updateList() {
    const ordered = [...cards].sort((a, b) => {
      const idxA = Number(a.dataset.index || 0);
      const idxB = Number(b.dataset.index || 0);
      if (activeSort === 'oldest') return idxB - idxA;
      if (activeSort === 'title') return (a.dataset.title || '').localeCompare(b.dataset.title || '', 'ja');
      if (activeSort === 'owner') {
        const cmp = (a.dataset.owner || '').localeCompare(b.dataset.owner || '', 'ja');
        return cmp || (idxA - idxB);
      }
      return idxA - idxB;
    });
    let shown = 0;
    const q = query.trim().toLocaleLowerCase();
    ordered.forEach((card) => {
      if (grid) grid.append(card);
      const matchOwner = activeOwner === 'all' || card.dataset.owner === activeOwner;
      const matchQuery = !q || `${card.dataset.title || ''} ${card.dataset.owner || ''}`.toLocaleLowerCase().includes(q);
      const ok = matchOwner && matchQuery;
      card.hidden = !ok;
      if (ok) shown += 1;
    });
    if (counter) counter.textContent = String(shown);
    let emptyEl = document.getElementById('filterEmpty');
    if (!shown && cards.length) {
      if (!emptyEl && grid) {
        emptyEl = document.createElement('p');
        emptyEl.id = 'filterEmpty';
        emptyEl.className = 'empty';
        emptyEl.textContent = '条件に一致するHTMLがありません。';
        grid.append(emptyEl);
      }
    } else if (emptyEl) {
      emptyEl.remove();
    }
  }

  if (chips) {
    chips.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      activeOwner = chip.dataset.owner || 'all';
      document.querySelectorAll('.chip').forEach((item) => {
        item.setAttribute('aria-pressed', String(item.dataset.owner === activeOwner));
      });
      updateList();
    });
  }

  if (searchInput) {
    searchInput.addEventListener('input', () => {
      query = searchInput.value || '';
      updateList();
    });
  }

  function closeSort() {
    if (!sortWrap || !sortMenu || !sortBtn) return;
    sortMenu.hidden = true;
    sortWrap.classList.remove('open');
    sortBtn.setAttribute('aria-expanded', 'false');
  }
  if (sortBtn && sortMenu) {
    sortBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const willOpen = sortMenu.hidden;
      sortMenu.hidden = !willOpen;
      sortWrap.classList.toggle('open', willOpen);
      sortBtn.setAttribute('aria-expanded', String(willOpen));
    });
    sortMenu.addEventListener('click', (e) => {
      const opt = e.target.closest('.cselect-opt');
      if (!opt) return;
      activeSort = opt.dataset.sort || 'newest';
      if (sortLabel) sortLabel.textContent = opt.textContent;
      sortMenu.querySelectorAll('.cselect-opt').forEach((o) => {
        o.setAttribute('aria-selected', String(o === opt));
      });
      closeSort();
      updateList();
    });
    document.addEventListener('click', (e) => {
      if (sortWrap && !sortWrap.contains(e.target)) closeSort();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSort();
    });
  }

  if (previewToggle) {
    previewToggle.addEventListener('click', () => {
      inlinePreview = !inlinePreview;
      previewToggle.setAttribute('aria-pressed', String(inlinePreview));
    });
  }
  if (grid && modal && modalFrame) {
    grid.addEventListener('click', (e) => {
      if (!inlinePreview) return;
      const card = e.target.closest('.card');
      if (!card) return;
      e.preventDefault();
      const href = card.getAttribute('href') || '#';
      const rawSrc = href.replace(/\/+$/, '') + '/content.html';
      if (modalTitle) modalTitle.textContent = card.dataset.title || 'HTML Preview';
      if (modalOpenTab) modalOpenTab.setAttribute('href', href);
      modalFrame.src = rawSrc;
      modal.showModal();
    });
    if (modalClose) {
      modalClose.addEventListener('click', () => {
        modal.close();
        modalFrame.src = 'about:blank';
      });
    }
    modal.addEventListener('close', () => { modalFrame.src = 'about:blank'; });
  }
})();
"""


@dataclass(frozen=True)
class HtmlConfig:
    html_dir: str = "html"
    pending_dir: str = "html-pending"
    catalog_file: str = "html_catalog.json"
    viewer_base_url: str = "http://127.0.0.1:8030"
    dashboard_url: str = "http://127.0.0.1:8765"
    max_bytes: int = 50 * 1024 * 1024
    require_approval: bool = True
    approver_id: int = 0
    thumbnail: bool = True
    chrome: str = ""


class HtmlStore:
    def __init__(self, storage, config: HtmlConfig):
        self.storage = storage
        self.config = config
        self.html_dir = storage.legacy_path(config.html_dir)
        self.pending_dir = storage.legacy_path(config.pending_dir)
        self.catalog_path = storage.legacy_path(config.catalog_file)
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.chrome = config.chrome or find_chrome()
        self.pending: dict[str, dict] = {}
        self.catalog = self._load_catalog()
        self.rebuild_dashboards()

    def _load_catalog(self):
        if not self.catalog_path.exists():
            return []
        try:
            data = json.loads(self.catalog_path.read_text("utf-8"))
        except OSError, ValueError:
            logger.exception("html_catalog.json の読み込みに失敗")
            return []
        if not isinstance(data, list):
            return []
        return [
            {str(key): str(value) for key, value in item.items()}
            for item in data
            if isinstance(item, dict)
        ]

    def save_catalog(self):
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(
            json.dumps(self.catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def title(data: bytes, fallback: str) -> str:
        text = data.decode("utf-8", errors="replace")
        match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        title = html_lib.unescape(match.group(1)) if match else ""
        title = " ".join(title.split())[:120]
        return title or Path(fallback).stem[:120] or "Untitled HTML"

    def site_dir(self, record: dict) -> Path:
        return self.html_dir / "users" / record.get("user_id", "") / record.get("site_id", "")

    def _cards(self, records: list[dict]) -> str:
        cards = []
        for idx, record in enumerate(records):
            site_id = html_lib.escape(record.get("site_id", ""), quote=True)
            user_id = html_lib.escape(record.get("user_id", ""), quote=True)
            raw_title = record.get("title", "Untitled HTML")
            label = html_lib.escape(raw_title)
            title_attr = html_lib.escape(raw_title, quote=True)
            name = record.get("display_name", "") or record.get("user_id", "")
            display_name = html_lib.escape(name)
            owner = html_lib.escape(name, quote=True)
            hue = _hue(name)
            has_thumb = (self.site_dir(record) / "thumb.jpg").is_file()
            thumb = (
                f'<img src="/users/{user_id}/{site_id}/thumb.jpg" alt="" loading="lazy" '
                'onload="this.parentElement.classList.add(\'has-thumb\')" '
                'onerror="this.parentElement.classList.add(\'no-thumb\')">'
                if has_thumb
                else ""
            )
            cards.append(
                f'<a class="card" href="/users/{user_id}/{site_id}/" target="_blank" '
                f'rel="noopener" data-index="{idx}" data-owner="{owner}" data-title="{title_attr}">'
                f'<span class="thumb" style="--h:{hue}">'
                f'{thumb}{_badge(record.get("site_id", ""))}</span>'
                f'<span class="card-body"><h3>{label}</h3>'
                f'<span class="meta"><span class="owner">'
                f'<span class="avatar" style="--h:{hue}"></span>'
                f'{display_name}</span>'
                f'<span class="date">{date_label(record)}</span></span></span></a>'
            )
        return "\n".join(cards)

    @staticmethod
    def _chips(records: list[dict]) -> str:
        owners: dict[str, int] = {}
        for record in records:
            name = record.get("display_name", "") or record.get("user_id", "")
            owners[name] = owners.get(name, 0) + 1
        items = [
            '<button class="chip" data-owner="all" aria-pressed="true">すべて '
            f'<span class="n">{len(records)}</span></button>'
        ]
        for name, count in sorted(owners.items(), key=lambda item: (-item[1], item[0])):
            items.append(
                f'<button class="chip" data-owner="{html_lib.escape(name, quote=True)}" '
                f'aria-pressed="false"><span class="avatar" style="--h:{_hue(name)}"></span>'
                f'{html_lib.escape(name)} <span class="n">{count}</span></button>'
            )
        return "\n".join(items)

    def dashboard_html(
        self,
        title: str,
        records: list[dict],
        back_href: str | None = None,
        *,
        page_url: str = "",
        og_author: str = "",
    ) -> str:
        safe_title = html_lib.escape(title)
        owners = {record.get("display_name", "") or record.get("user_id", "") for record in records}
        base = self.config.viewer_base_url.rstrip("/")
        preview = next(
            (rec for rec in records if self.thumbnail_path(rec).is_file()),
            records[0] if records else None,
        )
        image_url = ""
        if preview:
            p_uid = preview.get("user_id", "")
            p_sid = preview.get("site_id", "")
            if base:
                image_url = f"{base}/users/{p_uid}/{p_sid}/thumb.jpg"
            elif back_href:
                image_url = f"{p_sid}/thumb.jpg"
            else:
                image_url = f"users/{p_uid}/{p_sid}/thumb.jpg"
        if og_author:
            if records:
                latest_title = records[0].get("title", "HTML")
                og_desc = f"登録者: {og_author} ・ {len(records)}ページ（最新: {latest_title}）"
            else:
                og_desc = f"登録者: {og_author} のHTML一覧"
        elif records:
            latest_title = records[0].get("title", "HTML")
            latest_owner = records[0].get("display_name", "") or records[0].get("user_id", "")
            og_desc = f"登録者 {len(owners)}名 ・ {len(records)}ページ（最新: {latest_title} / {latest_owner}）"
        else:
            og_desc = "登録されたHTMLページのギャラリー"
        og_tags = _og_meta(
            title=title,
            description=og_desc,
            url=page_url,
            image_url=image_url,
            author=og_author,
        )
        back = ""
        if back_href:
            back = (
                f'<a class="backlink" href="{html_lib.escape(back_href, quote=True)}">'
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
                'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<path d="M15 5 8 12l7 7"/></svg>一覧へ戻る</a>'
            )
        chips = (
            f'<div class="chips" id="chips">{self._chips(records)}</div>'
            if len(owners) > 1
            else ""
        )
        body = self._cards(records) or '<p class="empty">まだHTMLがありません。</p>'
        dashboard_link = (
            f'<a class="btn" href="{html_lib.escape(self.config.dashboard_url, quote=True)}/">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M4 6h16v12H4z"/><path d="M4 10h16"/></svg>共通ボード</a>'
        )
        return f"""<!doctype html>
<html lang="ja" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{safe_title}</title>
{og_tags}
<script>try{{var t=localStorage.getItem("jllmHtmlTheme");document.documentElement.dataset.theme=t==="light"?"light":"dark";}}catch(_){{}}</script>
<style>{DASHBOARD_CSS}</style>
</head><body>
<div class="wrap">
  {back}
  <header class="topbar">
    <div class="brand-row">
      <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 6h16v12H4z"/><path d="M4 10h16"/></svg></span>
      <div class="brand-text">
        <span class="kicker">jllm voice</span>
        <h1>{safe_title}</h1>
      </div>
    </div>
    <div class="top-tools">
      <span class="stat-pill"><span class="stat-dot" aria-hidden="true"></span><b>{len(records)}</b> pages · <b>{len(owners)}</b> creators</span>
      <button id="previewToggle" class="btn" type="button" aria-pressed="false" title="クリックでページ内モーダル表示に切替">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6"/><path d="M1.8 5.8h12.4"/></svg>
        <span>モーダル表示</span>
      </button>
      <button id="themeBtn" class="btn" type="button" aria-label="ライトモードへ切替">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="2.8"/><path d="M8 1.6v1.5M8 12.9v1.5M1.6 8h1.5M12.9 8h1.5"/></svg>
        <span>ライト</span>
      </button>
      {dashboard_link}
    </div>
  </header>

  <section class="toolbar" aria-label="絞り込みと並び替え">
    <div class="search-box">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/></svg>
      <input id="searchInput" class="search-input" type="search" placeholder="タイトル・作成者で検索…" aria-label="タイトル・作成者で検索">
    </div>
    <div class="controls">
      <div class="cselect" id="sortSelect">
        <button id="sortBtn" class="cselect-btn" type="button" aria-haspopup="listbox" aria-expanded="false" aria-label="並び順">
          <span id="sortLabel">新着順</span>
          <svg viewBox="0 0 12 8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1.5 1.75 6 6.25l4.5-4.5"/></svg>
        </button>
        <div id="sortMenu" class="cselect-menu" role="listbox" hidden>
          <button class="cselect-opt" type="button" role="option" data-sort="newest" aria-selected="true">新着順</button>
          <button class="cselect-opt" type="button" role="option" data-sort="oldest" aria-selected="false">古い順</button>
          <button class="cselect-opt" type="button" role="option" data-sort="title" aria-selected="false">タイトル順</button>
          <button class="cselect-opt" type="button" role="option" data-sort="owner" aria-selected="false">作成者順</button>
        </div>
      </div>
      <div class="view-seg" role="group" aria-label="表示形式">
        <button type="button" data-view-mode="grid" aria-pressed="true" aria-label="グリッド表示">
          <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>
        </button>
        <button type="button" data-view-mode="list" aria-pressed="false" aria-label="リスト表示">
          <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="2.5" width="12" height="2.4" rx="1"/><rect x="2" y="6.8" width="12" height="2.4" rx="1"/><rect x="2" y="11.1" width="12" height="2.4" rx="1"/></svg>
        </button>
      </div>
    </div>
  </section>

  {chips}
  <section class="grid" id="grid">
    {body}
  </section>
  <footer>
    <span>{dashboard_link}</span>
    <span class="shown-count"><span id="shown">{len(records)}</span> / {len(records)} 件を表示</span>
  </footer>
</div>

<dialog id="viewerModal" class="viewer-modal" aria-labelledby="modalTitle">
  <div class="modal-shell">
    <div class="modal-head">
      <span id="modalTitle" class="modal-title">HTML Preview</span>
      <div class="modal-actions">
        <a id="modalOpenTab" class="btn" href="#" target="_blank" rel="noopener">新しいタブで開く</a>
        <button id="modalClose" class="btn" type="button">閉じる</button>
      </div>
    </div>
    <iframe id="modalFrame" class="modal-frame" title="HTML プレビュー"></iframe>
  </div>
</dialog>

<script>{DASHBOARD_JS}</script>
</body></html>"""

    @staticmethod
    def _write(path: Path, text: str):
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)

    def rebuild_dashboards(self):
        self.html_dir.mkdir(parents=True, exist_ok=True)
        base = self.config.viewer_base_url.rstrip("/")
        for record in self.catalog:
            site = self.site_dir(record)
            if (site / "content.html").is_file():
                self._write(
                    site / "index.html",
                    self.viewer_wrapper_html(
                        record.get("title", "HTML"),
                        record.get("user_id", ""),
                        record.get("site_id", ""),
                        author=record.get("display_name", "") or record.get("user_id", ""),
                        base_url=base,
                    ),
                )
        self._write(
            self.html_dir / "index.html",
            self.dashboard_html(
                "HTML Dashboard",
                list(reversed(self.catalog)),
                page_url=f"{base}/" if base else "",
            ),
        )
        by_user: dict[str, list[dict]] = {}
        for record in self.catalog:
            by_user.setdefault(record.get("user_id", "unknown"), []).append(record)
        for user_id, records in by_user.items():
            user_dir = self.html_dir / "users" / user_id
            user_dir.mkdir(parents=True, exist_ok=True)
            display_name = records[-1].get("display_name", user_id)
            self._write(
                user_dir / "index.html",
                self.dashboard_html(
                    f"{display_name} のHTML",
                    list(reversed(records)),
                    "/",
                    page_url=f"{base}/users/{user_id}/" if base else "",
                    og_author=display_name,
                ),
            )

    def thumbnail_path(self, record: dict) -> Path:
        return self.site_dir(record) / "thumb.jpg"

    async def make_thumbnail(self, record: dict) -> bool:
        if not self.config.thumbnail or not self.chrome:
            return False
        site = self.site_dir(record)
        source = site / "content.html"
        if not source.is_file():
            return False
        ok = await capture_thumbnail(self.chrome, source, self.thumbnail_path(record))
        if ok:
            logger.info("サムネイルを生成: %s", record.get("site_id"))
        return ok

    def schedule_thumbnails(self, records: list[dict] | None = None):
        """足りないサムネイルを裏のスレッドで順に作る（イベントループ不要）。"""

        if not self.config.thumbnail or not self.chrome:
            logger.info("サムネイル生成は無効（chrome=%s）", self.chrome)
            return None
        targets = [
            record
            for record in (records if records is not None else self.catalog)
            if not self.thumbnail_path(record).is_file()
        ]
        if not targets:
            return None
        thread = threading.Thread(
            target=self._thumbnail_thread, args=(targets,), name="html-thumbnails", daemon=True
        )
        thread.start()
        return thread

    def _thumbnail_thread(self, targets: list[dict]):
        with _THUMB_LOCK:
            asyncio.run(self._thumbnail_worker(targets))

    async def _thumbnail_worker(self, targets: list[dict]):
        pending = list(targets)
        for _attempt in (1, 2):
            failed: list[dict] = []
            for record in pending:
                try:
                    if await asyncio.wait_for(self.make_thumbnail(record), 120):
                        self.rebuild_dashboards()
                        continue
                except TimeoutError:
                    logger.warning("サムネイル生成がタイムアウト: %s", record.get("site_id"))
                except Exception:
                    logger.exception("サムネイル生成に失敗: %s", record.get("site_id"))
                failed.append(record)
            pending = failed
            if not pending:
                break
            await asyncio.sleep(1.0)
        if pending:
            logger.warning(
                "サムネイルを作れなかったページ: %s",
                ", ".join(record.get("site_id", "?") for record in pending),
            )

    @staticmethod
    def viewer_wrapper_html(
        title: str,
        user_id: str,
        site_id: str,
        data: bytes = b"",
        *,
        author: str = "",
        base_url: str = "",
    ) -> str:
        safe_title = html_lib.escape(title)
        safe_user = html_lib.escape(user_id, quote=True)
        base = base_url.rstrip("/")
        page_url = f"{base}/users/{user_id}/{site_id}/" if base else ""
        image_url = f"{base}/users/{user_id}/{site_id}/thumb.jpg" if base else "thumb.jpg"
        description = f"登録者: {author}" if author else f"HTMLページ: {title}"
        og_tags = _og_meta(
            title=title,
            description=description,
            url=page_url,
            image_url=image_url,
            author=author,
        )
        return (
            "<!doctype html>\n"
            '<html lang="ja"><head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            f"<title>{safe_title}</title>\n"
            f"{og_tags}\n"
            '<style id="html-viewer-style">\n'
            "*{box-sizing:border-box}\n"
            "html,body{margin:0;padding:0;height:100%;background:#101216;color:#f4f4f5;"
            'font:13px/1.3 ui-sans-serif,system-ui,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif}\n'
            ".html-viewer-shell{display:flex;flex-direction:column;height:100dvh;width:100%;overflow:hidden}\n"
            "#html-viewer-nav{flex:none;display:flex;align-items:center;gap:6px;min-height:42px;padding:4px 10px;"
            "background:#171a20;color:#f4f4f5;border-bottom:1px solid rgba(255,255,255,.1)}\n"
            "#html-viewer-nav[hidden]{display:none}\n"
            "#html-viewer-nav a,#html-viewer-nav button{display:inline-flex;align-items:center;gap:5px;"
            "min-height:30px;padding:0 9px;border-radius:4px;border:1px solid rgba(255,255,255,.14);"
            "background:#1d2129;color:#d4d4d8;text-decoration:none;font:inherit;font-size:12px;font-weight:600;"
            "cursor:pointer;white-space:nowrap}\n"
            "#html-viewer-nav a:hover,#html-viewer-nav button:hover{border-color:#f59e0b;color:#fbbf24}\n"
            "#html-viewer-nav svg{width:13px;height:13px;flex:none}\n"
            "#html-viewer-nav .html-viewer-title{margin-left:4px;color:#9ca3af;overflow:hidden;"
            "text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}\n"
            ".html-viewer-frame{flex:1 1 auto;width:100%;height:100%;min-height:0;border:0;display:block;background:#fff}\n"
            "#html-viewer-restore{position:fixed;top:8px;right:10px;z-index:20;display:none;"
            "align-items:center;gap:4px;min-height:28px;padding:0 9px;border-radius:4px;"
            "border:1px solid rgba(255,255,255,.22);background:rgba(23,26,32,.88);color:#f4f4f5;"
            "font:inherit;font-size:11.5px;font-weight:600;cursor:pointer}\n"
            "#html-viewer-restore[hidden]{display:none}\n"
            "@media (max-width:600px){#html-viewer-nav .html-viewer-title{display:none}"
            "#html-viewer-nav a span,#html-viewer-nav button span{display:none}}\n"
            "</style>\n"
            "</head><body>\n"
            '<div class="html-viewer-shell">\n'
            '  <nav id="html-viewer-nav">\n'
            '    <a href="/" title="HTML Dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M15 5 8 12l7 7"/></svg><span>HTML Dashboard</span></a>\n'
            f'    <a href="/users/{safe_user}/" title="ユーザーページ"><svg viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3.2-6 7-6s7 2 7 6"/></svg>'
            "<span>ユーザーページ</span></a>\n"
            f'    <span class="html-viewer-title">{safe_title}</span>\n'
            '    <a href="content.html" title="枠なしで直接開く"><svg viewBox="0 0 16 16" fill="none" '
            'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M9.5 2.5h4v4M13.5 2.5 8 8M6.5 4H3.5a1 1 0 0 0-1 1v7.5a1 1 0 0 0 1 1H11a1 1 0 0 0 1-1v-3"/></svg>'
            "<span>元HTMLのみ</span></a>\n"
            '    <button id="html-viewer-hide" type="button" title="上のバーを隠す"><svg viewBox="0 0 16 16" fill="none" '
            'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">'
            '<path d="M4 10 8 6l4 4"/></svg><span>隠す</span></button>\n'
            "  </nav>\n"
            f'  <iframe class="html-viewer-frame" src="content.html" title="{safe_title}" allowfullscreen></iframe>\n'
            "</div>\n"
            '<button id="html-viewer-restore" type="button" hidden>バーを表示</button>\n'
            "<script>(function(){"
            "var nav=document.getElementById('html-viewer-nav');"
            "var hideBtn=document.getElementById('html-viewer-hide');"
            "var restoreBtn=document.getElementById('html-viewer-restore');"
            "if(!nav)return;"
            "var embedded=false;try{embedded=window.self!==window.top;}catch(e){embedded=true;}"
            "if(embedded||/[?&]embed=1(&|$)/.test(location.search)){nav.hidden=true;return;}"
            "if(hideBtn&&restoreBtn){"
            "hideBtn.addEventListener('click',function(){nav.hidden=true;restoreBtn.hidden=false;restoreBtn.style.display='inline-flex';});"
            "restoreBtn.addEventListener('click',function(){nav.hidden=false;restoreBtn.hidden=true;restoreBtn.style.display='none';});"
            "}"
            "})();</script>\n"
            "</body></html>\n"
        )

    def register(self, data: bytes, filename: str, author) -> str:
        request_id = uuid.uuid4().hex[:12]
        path = self.pending_dir / f"{request_id}.html"
        path.write_bytes(data)
        self.pending[request_id] = {
            "path": str(path),
            "site_id": request_id,
            "user_id": str(author.id),
            "display_name": str(author.display_name),
            "title": self.title(data, filename or "HTML"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        return request_id

    def publish(self, request_id: str) -> str:
        request = self.pending.pop(request_id, None)
        if request is None:
            return "このHTML登録申請は期限切れです。"
        source = Path(request["path"])
        if not source.is_file():
            return "登録用HTMLが見つかりません。"
        site_id = request["site_id"]
        user_id = request["user_id"]
        target_dir = self.html_dir / "users" / user_id / site_id
        target_dir.mkdir(parents=True, exist_ok=False)
        source.replace(target_dir / "content.html")
        raw = (target_dir / "content.html").read_bytes()
        self._write(
            target_dir / "index.html",
            self.viewer_wrapper_html(
                request["title"],
                user_id,
                site_id,
                raw,
                author=request["display_name"] or user_id,
                base_url=self.config.viewer_base_url,
            ),
        )
        record = {
            "site_id": site_id,
            "user_id": user_id,
            "display_name": request["display_name"],
            "title": request["title"],
            "created_at": request["created_at"],
        }
        self.catalog.append(record)
        self.save_catalog()
        self.rebuild_dashboards()
        self.schedule_thumbnails([record])
        return f"HTMLを登録しました。\n{self.config.viewer_base_url}/users/{user_id}/{site_id}/"

    def reject(self, request_id: str) -> str:
        request = self.pending.pop(request_id, None)
        if request is not None:
            Path(request["path"]).unlink(missing_ok=True)
        return "HTML登録申請を拒否しました。"

    def expire(self, request_id: str):
        request = self.pending.pop(request_id, None)
        if request is not None:
            Path(request["path"]).unlink(missing_ok=True)


class Plugin:
    async def setup(self, ctx):
        config = HtmlConfig(
            html_dir=ctx.config.get("html_dir", "html"),
            pending_dir=ctx.config.get("pending_dir", "html-pending"),
            catalog_file=ctx.config.get("catalog_file", "html_catalog.json"),
            viewer_base_url=ctx.config.get("viewer_base_url", "http://127.0.0.1:8030"),
            dashboard_url=ctx.config.get("dashboard_url", "http://127.0.0.1:8765"),
            max_bytes=ctx.config.get("max_bytes", 50 * 1024 * 1024),
            require_approval=ctx.config.get("require_approval", True),
            approver_id=ctx.config.get("approver_id", 0),
            thumbnail=ctx.config.get("thumbnail", True),
            chrome=ctx.config.get("chrome", ""),
        )
        self.store = HtmlStore(ctx.dependencies["storage.local"], config)
        self.store.schedule_thumbnails()
        return {"feature.html-library": self.store}

    async def start(self):
        pass

    async def stop(self):
        pass
