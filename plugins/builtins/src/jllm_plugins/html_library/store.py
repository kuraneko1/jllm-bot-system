import html as html_lib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


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


class HtmlStore:
    def __init__(self, storage, config: HtmlConfig):
        self.storage = storage
        self.config = config
        self.html_dir = storage.legacy_path(config.html_dir)
        self.pending_dir = storage.legacy_path(config.pending_dir)
        self.catalog_path = storage.legacy_path(config.catalog_file)
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
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

    def dashboard_html(self, title: str, records: list[dict], back_href: str | None = None) -> str:
        cards = []
        for record in records:
            site_id = html_lib.escape(record.get("site_id", ""), quote=True)
            user_id = html_lib.escape(record.get("user_id", ""), quote=True)
            label = html_lib.escape(record.get("title", "Untitled HTML"))
            display_name = html_lib.escape(record.get("display_name", user_id))
            initial = html_lib.escape(label[:1] or "H")
            cards.append(
                f'<a class="tile" href="/users/{user_id}/{site_id}/">'
                f'<div class="icon">{initial}</div><div class="tile-title">{label}</div>'
                f'<div class="tile-user">{display_name}</div></a>'
            )
        back = (
            f'<p><a href="{html_lib.escape(back_href, quote=True)}">← 戻る</a></p>'
            if back_href
            else ""
        )
        body = "\n".join(cards) or "<p class='empty'>まだHTMLがありません。</p>"
        safe_title = html_lib.escape(title)
        nav = (
            f'<nav class="dashboard-nav"><a href="'
            f'{html_lib.escape(self.config.dashboard_url, quote=True)}/">'
            "🔊 共通ボード（音声・辞書・GIF）</a></nav>"
        )
        return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#10131a;color:#eef2ff}}
body{{max-width:1100px;margin:0 auto;padding:32px}}
h1{{font-size:clamp(1.5rem,3vw,2.5rem);margin:0 0 24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px}}
.tile{{display:block;color:inherit;text-decoration:none;background:#1a2030;
border:1px solid #30394e;border-radius:16px;padding:16px;min-height:150px;transition:.15s}}
.tile:hover{{border-color:#8ea7ff;transform:translateY(-2px)}}
.icon{{width:64px;height:64px;border-radius:16px;display:grid;place-items:center;background:#4054a5;color:white;font-size:2rem;font-weight:700;margin-bottom:18px}}
.tile-title{{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tile-user,.empty{{color:#aeb9d5;font-size:.9rem;margin-top:8px}}
a{{color:#a9bcff}}.dashboard-nav{{margin:-10px 0 20px}}.dashboard-nav a{{font-weight:700}}
</style></head><body><h1>{safe_title}</h1>{nav}{back}<div class="grid">{body}</div></body></html>"""

    def rebuild_dashboards(self):
        self.html_dir.mkdir(parents=True, exist_ok=True)
        (self.html_dir / "index.html").write_text(
            self.dashboard_html("HTML Dashboard", list(reversed(self.catalog))),
            encoding="utf-8",
        )
        by_user: dict[str, list[dict]] = {}
        for record in self.catalog:
            by_user.setdefault(record.get("user_id", "unknown"), []).append(record)
        for user_id, records in by_user.items():
            user_dir = self.html_dir / "users" / user_id
            user_dir.mkdir(parents=True, exist_ok=True)
            display_name = records[-1].get("display_name", user_id)
            (user_dir / "index.html").write_text(
                self.dashboard_html(f"{display_name} のHTML", list(reversed(records)), "/"),
                encoding="utf-8",
            )

    @staticmethod
    def viewer_wrapper_html(title: str, user_id: str, site_id: str, data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        safe_title = html_lib.escape(title)
        safe_user = html_lib.escape(user_id, quote=True)
        style = (
            '<style id="html-viewer-style">'
            "#html-viewer-nav{position:fixed;top:0;left:0;right:0;z-index:2147483647;"
            "display:flex;align-items:center;gap:14px;min-height:46px;padding:8px 16px;"
            "background:#171d2bdd;color:#eef2ff;border-bottom:1px solid #53617d;"
            "font:14px system-ui,sans-serif;box-sizing:border-box;backdrop-filter:blur(8px)}"
            "#html-viewer-nav a{color:#c4d1ff;text-decoration:none;font-weight:700}"
            "#html-viewer-nav a:hover{text-decoration:underline}"
            "#html-viewer-nav .html-viewer-title{overflow:hidden;text-overflow:ellipsis;"
            "white-space:nowrap;color:#eef2ff}"
            "</style>"
        )
        nav = (
            '<nav id="html-viewer-nav">'
            '<a href="/">← HTML Dashboard</a>'
            f'<a href="/users/{safe_user}/">ユーザーページ</a>'
            f'<span class="html-viewer-title">{safe_title}</span></nav>'
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
        (target_dir / "index.html").write_text(
            self.viewer_wrapper_html(request["title"], user_id, site_id, raw),
            encoding="utf-8",
        )
        self.catalog.append(
            {
                "site_id": site_id,
                "user_id": user_id,
                "display_name": request["display_name"],
                "title": request["title"],
                "created_at": request["created_at"],
            }
        )
        self.save_catalog()
        self.rebuild_dashboards()
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
        )
        self.store = HtmlStore(ctx.dependencies["storage.local"], config)
        return {"feature.html-library": self.store}

    async def start(self):
        pass

    async def stop(self):
        pass
