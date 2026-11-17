"""検索ツールの実体（RustXNG の検索と r.jina.ai の本文取得）。

モデルには OpenAI 互換の function calling で渡す。戻り値は文字列で、
失敗も例外にせず「検索できませんでした」という文にして返す（呼び出し側で
tool メッセージにする）。
"""

from __future__ import annotations

import json
import time

import aiohttp

from .web_content import WebContentError, validate_url

#: 検索結果全体の上限。モデルの文脈とコストを守る。
MAX_SEARCH_CHARS = 3000
#: 1 件あたりの抜粋。
MAX_SNIPPET_CHARS = 300
#: read_page の上限。
MAX_PAGE_CHARS = 8000
#: 1 ターンあたりの呼び出し上限（暴走よけ）。
MAX_CALLS = 4
MAX_PAGE_CALLS = 2

_TIME_RANGES = ("day", "week", "month", "year")
_LOCAL_HOSTS = ("localhost", "localhost.localdomain")


def _host_of(value) -> str:
    """URL のホスト名（小文字）。読めなければ空。単一ラベルの社内名も弾く。"""
    text = str(value or "").strip()

    if "//" not in text:
        return ""

    try:
        from urllib.parse import urlsplit

        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""

    if host and "." not in host:
        return ""

    return host

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Web を検索して上位結果のタイトル・URL・抜粋を返す。"
                "最新情報や知らない固有名詞を確認するときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索語（日本語可）"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "返す件数（既定5）",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": list(_TIME_RANGES),
                        "description": "新着に絞るときだけ",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": (
                "検索結果の URL など、特定ページの本文を Markdown で読む。"
                "抜粋だけでは足りないときに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s) の URL"},
                },
                "required": ["url"],
            },
        },
    },
]


class SearchToolError(ValueError):
    pass


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def format_results(results) -> str:
    """検索結果をモデル向けの箇条書きにする。"""
    lines = []
    total = 0

    for index, item in enumerate(results or (), 1):
        if not isinstance(item, dict):
            continue

        title = _clean(item.get("title"), 120) or "(無題)"
        url = str(item.get("url") or "").strip()

        if not url:
            continue

        snippet = _clean(item.get("content"), MAX_SNIPPET_CHARS)
        block = f"{index}. {title}\n   {url}"

        if snippet:
            block += f"\n   {snippet}"

        if total + len(block) > MAX_SEARCH_CHARS:
            break

        lines.append(block)
        total += len(block)

    return "\n".join(lines)


class SearchTools:
    """検索と本文取得。1 インスタンスをプラグインで共有する。"""

    def __init__(
        self,
        endpoint="http://127.0.0.1:29483",
        *,
        max_results=5,
        timeout=8.0,
        cache_seconds=60.0,
        fetch_endpoint="https://r.jina.ai",
        fetch_timeout=20.0,
        fetch_max_chars=MAX_PAGE_CHARS,
        fetch_cache_seconds=600.0,
        fetch_api_key="",
        fetcher=None,
        max_rounds=3,
        max_calls=MAX_CALLS,
        max_page_calls=MAX_PAGE_CALLS,
    ):
        self.endpoint = str(endpoint).rstrip("/")
        self.max_results = max(1, min(10, int(max_results)))
        self.timeout = float(timeout)
        self.cache_seconds = float(cache_seconds)
        self.fetch_endpoint = str(fetch_endpoint).rstrip("/")
        self.fetch_timeout = float(fetch_timeout)
        self.fetch_max_chars = int(fetch_max_chars)
        self.fetch_cache_seconds = float(fetch_cache_seconds)
        self.fetch_api_key = str(fetch_api_key or "").strip()
        self.fetcher = fetcher
        self.max_rounds = max(1, int(max_rounds))
        self.max_calls = max(1, int(max_calls))
        self.max_page_calls = max(1, int(max_page_calls))
        self._cache: dict[tuple, tuple[float, str]] = {}
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------ セッション

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "JLLM-Voice/1.0 (search tool)"},
            )

        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

        self._session = None

    # ------------------------------------------------------------ キャッシュ

    def _cached(self, key) -> str | None:
        entry = self._cache.get(key)

        if entry is None:
            return None

        expires, value = entry

        if expires < time.monotonic():
            self._cache.pop(key, None)
            return None

        return value

    def _store(self, key, value: str, seconds: float) -> None:
        if len(self._cache) > 128:
            now = time.monotonic()
            self._cache = {
                k: v for k, v in self._cache.items() if v[0] >= now
            }

        self._cache[key] = (time.monotonic() + seconds, value)

    # ------------------------------------------------------------ 検索

    async def web_search(self, query, max_results=None, time_range=None) -> str:
        query = _clean(query, 300)

        if not query:
            raise SearchToolError("検索語が空です。")

        try:
            count = int(max_results or self.max_results)
        except (TypeError, ValueError):
            count = self.max_results

        count = max(1, min(10, count))
        span = str(time_range or "").strip().lower()
        span = span if span in _TIME_RANGES else None
        key = ("search", query, count, span)
        cached = self._cached(key)

        if cached is not None:
            return cached

        params = {"q": query, "format": "json", "max_results": count}

        if span:
            params["time_range"] = span

        try:
            data = await self._json(f"{self.endpoint}/search", params)

        except TimeoutError as exc:
            raise SearchToolError("検索がタイムアウトしました。") from exc

        except aiohttp.ClientError as exc:
            raise SearchToolError("検索サービスに接続できませんでした。") from exc

        except ValueError as exc:
            raise SearchToolError("検索結果を読めませんでした。") from exc

        text = format_results(data.get("results") if isinstance(data, dict) else None)
        text = text or "検索結果は 0 件でした。"
        self._store(key, text, self.cache_seconds)
        return text

    async def _json(self, url, params):
        async with self._client().get(url, params=params) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    # ------------------------------------------------------------ 本文取得

    async def read_page(self, url) -> str:
        host = _host_of(url)

        if not host or host in _LOCAL_HOSTS or host.endswith((".local", ".internal")):
            raise SearchToolError("公開されているページの URL を指定してください。")

        try:
            target = str(validate_url(url))
        except WebContentError as exc:
            raise SearchToolError(str(exc)) from exc

        key = ("page", target)
        cached = self._cached(key)

        if cached is not None:
            return cached

        text = None

        try:
            text = await self._jina(target)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            text = None

        if not text and self.fetcher is not None:
            try:
                page = await self.fetcher.fetch(target)
                title = str(page.get("title") or "").strip()
                body = str(page.get("text") or "").strip()
                text = (f"Title: {title}\n\n{body}" if title else body) or None
            except (WebContentError, aiohttp.ClientError, TimeoutError, ValueError):
                text = None

        if not text:
            raise SearchToolError("ページ本文を取得できませんでした。")

        text = text[: self.fetch_max_chars]
        self._store(key, text, self.fetch_cache_seconds)
        return text

    async def _jina(self, target: str) -> str | None:
        headers = {
            "Accept": "text/plain",
            "X-Timeout": str(int(self.fetch_timeout)),
            "X-Return-Format": "markdown",
        }

        if self.fetch_api_key:
            headers["Authorization"] = f"Bearer {self.fetch_api_key}"

        url = f"{self.fetch_endpoint}/{target}"
        timeout = aiohttp.ClientTimeout(total=self.fetch_timeout + 5)

        async with self._client().get(url, headers=headers, timeout=timeout) as response:
            if response.status == 429:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=429
                )

            response.raise_for_status()
            body = await response.text()

        body = body.strip()
        return body or None

    # ------------------------------------------------------------ 実行

    async def run(self, name: str, arguments) -> str:
        """tool_calls の 1 件を実行する。失敗は文にして返す。"""
        args = arguments

        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {}

        if not isinstance(args, dict):
            args = {}

        try:
            if name == "web_search":
                return await self.web_search(
                    args.get("query"),
                    args.get("max_results"),
                    args.get("time_range"),
                )

            if name == "read_page":
                return await self.read_page(args.get("url"))

            return f"不明なツールです: {name}"

        except SearchToolError as exc:
            return f"ツールを実行できませんでした: {exc}"

        except Exception as exc:
            return f"ツールでエラーが発生しました: {type(exc).__name__}"
