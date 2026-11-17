
import asyncio
import base64
import html as html_lib
import json
import logging
import re
import shutil
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

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
            await command(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
            source_uri = await asyncio.to_thread(lambda: source.absolute().as_uri())
            await command("Page.navigate", {"url": source_uri})
            await asyncio.sleep(wait)
            shot = await command("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
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
:root{--bg:#0b0e14;--panel:#121826;--panel2:#161d2c;--line:rgba(255,255,255,.085);
--text:#e9eefc;--muted:#8d98b4;--accent:#8aa4ff;--radius:16px;color-scheme:dark}
body{margin:0;background:
radial-gradient(1100px 520px at 18% -12%, #1a2340 0%, transparent 62%),
radial-gradient(900px 500px at 92% 4%, #211a35 0%, transparent 58%), var(--bg);
color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit}
.wrap{max-width:1200px;margin:0 auto;padding:34px 26px 90px}
.brand{display:flex;align-items:center;gap:12px}
.mark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;
background:linear-gradient(140deg,#a9bbff,#6f86e8);color:#0b0e14}
.mark svg{width:19px;height:19px}
.kicker{color:var(--muted);font-size:.74rem;letter-spacing:.16em;text-transform:uppercase}
h1{font-size:clamp(1.6rem,3vw,2.15rem);margin:14px 0 8px;letter-spacing:-.02em;font-weight:680}
.sub{color:var(--muted);margin:0;font-size:.92rem;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.sub b{color:var(--text);font-weight:600}
.dot{width:4px;height:4px;border-radius:50%;background:currentColor;opacity:.5}
.backlink{display:inline-flex;align-items:center;gap:7px;color:var(--muted);text-decoration:none;
font-size:.82rem;font-weight:600;margin-bottom:6px;padding:5px 10px 5px 6px;border-radius:9px}
.backlink:hover{color:var(--text);background:rgba(255,255,255,.05)}
.backlink svg{width:15px;height:15px}
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:20px}
.chip{display:inline-flex;align-items:center;gap:8px;padding:7px 13px;border-radius:999px;
border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted);
font:inherit;font-size:.84rem;cursor:pointer;transition:.16s}
.chip:hover{color:var(--text);border-color:rgba(138,164,255,.4)}
.chip[aria-pressed="true"]{background:linear-gradient(135deg,#cfdaff,#8aa4ff);color:#0b0e14;
border-color:transparent;font-weight:700}
.chip .n{font-variant-numeric:tabular-nums;opacity:.62}
.chip .avatar{width:18px;height:18px;border-radius:50%;background:hsl(var(--h) 55% 55%)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:16px;margin-top:18px}
.card{display:flex;flex-direction:column;border-radius:var(--radius);overflow:hidden;
border:1px solid var(--line);background:linear-gradient(180deg,var(--panel2),var(--panel));
text-decoration:none;color:inherit;transition:transform .18s ease,border-color .18s,box-shadow .18s}
.card:hover{transform:translateY(-3px);border-color:rgba(138,164,255,.45);
box-shadow:0 16px 36px rgba(0,0,0,.42)}
.thumb{position:relative;display:grid;place-items:center;aspect-ratio:16/10;overflow:hidden;
background:linear-gradient(145deg,hsl(var(--h) 42% 24%),hsl(calc(var(--h) + 30) 40% 12%))}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
object-position:top center;opacity:0;transition:opacity .35s ease;display:block}
.thumb.has-thumb img{opacity:1}
.thumb .badge{width:56px;height:56px;border-radius:16px}
.thumb .badge svg{width:28px;height:28px}
.thumb.has-thumb .badge{display:none}
.card-body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:7px;min-width:0}
.card h3{margin:0;font-size:.93rem;font-weight:640;line-height:1.42;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .meta{display:flex;align-items:center;gap:10px;min-width:0}
.card .owner{color:var(--muted);font-size:.78rem;display:flex;align-items:center;gap:7px;
min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .date{margin-left:auto;flex:0 0 auto;color:var(--muted);font-size:.74rem;
font-variant-numeric:tabular-nums;opacity:.8}
.card .owner .avatar{width:16px;height:16px;border-radius:50%;flex:0 0 auto;
background:linear-gradient(140deg,hsl(var(--h) 62% 58%),hsl(calc(var(--h) + 40) 60% 40%))}
.empty{color:var(--muted);padding:40px 0;text-align:center}
footer{margin-top:44px;color:var(--muted);font-size:.78rem;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
footer a{display:inline-flex;align-items:center;gap:7px;color:var(--muted);text-decoration:none;
padding:6px 10px;border-radius:9px;border:1px solid var(--line)}
footer a:hover{color:var(--text);border-color:rgba(138,164,255,.4)}
footer a svg{width:15px;height:15px}
@media (max-width:640px){.wrap{padding:22px 16px 70px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

DASHBOARD_JS = r"""
const chips = document.getElementById('chips');
const cards = [...document.querySelectorAll('.card')];
const counter = document.getElementById('shown');
if (chips) chips.addEventListener('click', (event) => {
  const chip = event.target.closest('.chip');
  if (!chip) return;
  const owner = chip.dataset.owner;
  document.querySelectorAll('.chip').forEach((item) => {
    item.setAttribute('aria-pressed', String(item.dataset.owner === owner));
  });
  let shown = 0;
  cards.forEach((card) => {
    const ok = owner === 'all' || card.dataset.owner === owner;
    card.hidden = !ok;
    if (ok) shown += 1;
  });
  if (counter) counter.textContent = String(shown);
});
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
        for record in records:
            site_id = html_lib.escape(record.get("site_id", ""), quote=True)
            user_id = html_lib.escape(record.get("user_id", ""), quote=True)
            label = html_lib.escape(record.get("title", "Untitled HTML"))
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
                f'rel="noopener" data-owner="{owner}">'
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

    def dashboard_html(self, title: str, records: list[dict], back_href: str | None = None) -> str:
        safe_title = html_lib.escape(title)
        owners = {record.get("display_name", "") or record.get("user_id", "") for record in records}
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
            f'<a href="{html_lib.escape(self.config.dashboard_url, quote=True)}/">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M4 6h16v12H4z"/><path d="M4 10h16"/></svg>共通ボード</a>'
        )
        return f"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>{DASHBOARD_CSS}</style>
</head><body>
<div class="wrap">
  <header>
    {back}
    <div class="brand">
      <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 6h16v12H4z"/><path d="M4 10h16"/></svg></span>
      <span class="kicker">jllm voice</span>
    </div>
    <h1>{safe_title}</h1>
    <p class="sub"><span><b>{len(records)}</b> pages</span><span class="dot"></span>
      <span><b>{len(owners)}</b> creators</span></p>
  </header>
  {chips}
  <section class="grid" id="grid">
    {body}
  </section>
  <footer>{dashboard_link}<span class="dot"></span>
    <span><span id="shown">{len(records)}</span> / {len(records)} 件を表示</span></footer>
</div>
<script>{DASHBOARD_JS}</script>
</body></html>"""

    @staticmethod
    def _write(path: Path, text: str):
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)

    def rebuild_dashboards(self):
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self._write(
            self.html_dir / "index.html",
            self.dashboard_html("HTML Dashboard", list(reversed(self.catalog))),
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
                self.dashboard_html(f"{display_name} のHTML", list(reversed(records)), "/"),
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
    def viewer_wrapper_html(title: str, user_id: str, site_id: str, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        safe_title = html_lib.escape(title)
        safe_user = html_lib.escape(user_id, quote=True)
        style = (
            '<style id="html-viewer-style">'
            "#html-viewer-nav{position:sticky;top:0;z-index:2147483647;display:flex;"
            "align-items:center;gap:4px;min-height:44px;padding:0 10px;box-sizing:border-box;"
            "background:rgba(15,20,30,.92);color:#e9eefc;"
            "border-bottom:1px solid rgba(255,255,255,.09);"
            'font:13.5px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;'
            "backdrop-filter:blur(10px);transition:transform .22s ease,opacity .22s ease}"
            "#html-viewer-nav.is-hidden{transform:translateY(-100%);opacity:0;pointer-events:none}"
            "#html-viewer-nav a{display:inline-flex;align-items:center;gap:6px;color:#c3d0ff;"
            "text-decoration:none;font-weight:600;padding:7px 9px;border-radius:9px}"
            "#html-viewer-nav a:hover{background:rgba(255,255,255,.08);color:#eef2ff}"
            "#html-viewer-nav a svg{width:14px;height:14px;flex:0 0 auto}"
            "#html-viewer-nav .html-viewer-title{margin-left:8px;overflow:hidden;"
            "text-overflow:ellipsis;white-space:nowrap;opacity:.85}"
            "#html-viewer-nav .html-viewer-gap{flex:1}"
            "@media (max-width:560px){#html-viewer-nav .html-viewer-title{display:none}"
            "#html-viewer-nav a span{display:none}}"
            "</style>"
        )
        nav = (
            '<nav id="html-viewer-nav">'
            '<a href="/"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true"><path d="M15 5 8 12l7 7"/>'
            "</svg><span>HTML Dashboard</span></a>"
            f'<a href="/users/{safe_user}/"><svg viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3.2-6 7-6s7 2 7 6"/></svg>'
            "<span>ユーザーページ</span></a>"
            '<span class="html-viewer-gap"></span>'
            f'<span class="html-viewer-title">{safe_title}</span></nav>'
        )
        script = (
            "<script>(function(){"
            "var nav=document.getElementById('html-viewer-nav');if(!nav)return;"
            "var embedded=false;try{embedded=window.self!==window.top;}catch(e){embedded=true;}"
            "if(embedded||/[?&]embed=1(&|$)/.test(location.search)){nav.remove();return;}"
            "var last=0;addEventListener('scroll',function(){var y=window.scrollY||0;"
            "if(y<=6){nav.classList.remove('is-hidden');}"
            "else if(y>last+6){nav.classList.add('is-hidden');}"
            "else if(y<last-6){nav.classList.remove('is-hidden');}last=y;},{passive:true});"
            "})();</script>"
        )
        head = re.search(r"</head\s*>", text, flags=re.IGNORECASE)
        if head:
            text = text[: head.start()] + style + text[head.start() :]
        else:
            text = style + text
        body = re.search(r"<body[^>]*>", text, flags=re.IGNORECASE)
        if body:
            text = text[: body.end()] + nav + text[body.end() :]
        else:
            text = nav + text
        tail = text.rfind("</body>")
        if tail != -1:
            text = text[:tail] + script + text[tail:]
        else:
            text = text + script
        return text

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
            self.viewer_wrapper_html(request["title"], user_id, site_id, raw),
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
