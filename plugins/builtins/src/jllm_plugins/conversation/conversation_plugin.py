from .conversation import ConversationService
from .search_mode import SearchModeStore
from .search_tools import SearchTools


class Plugin:
    async def setup(self, ctx):
        config = dict(ctx.config or {})
        options = dict(config.get("search") or {})
        fetcher = ctx.dependencies.get("web.safe-fetch")
        search = None
        mode = None

        if options:
            fetch = dict(options.get("fetch") or {})
            mode = SearchModeStore(
                config.get("state_file", "data/search_tool.json"),
                bool(options.get("enabled", False)),
            )
            search = SearchTools(
                options.get("endpoint", "http://127.0.0.1:29483"),
                max_results=options.get("max_results", 5),
                timeout=options.get("timeout", 8),
                cache_seconds=options.get("cache_seconds", 60),
                fetch_endpoint=fetch.get("endpoint", "https://r.jina.ai"),
                fetch_timeout=fetch.get("timeout", 20),
                fetch_max_chars=fetch.get("max_chars", 8000),
                fetch_cache_seconds=fetch.get("cache_seconds", 600),
                fetch_api_key=_key(fetch.get("api_key_file")),
                fetcher=fetcher if fetch.get("fallback_safe_fetch", True) else None,
                max_rounds=options.get("max_rounds", 3),
            )
        self.search = search
        self.service = ConversationService(
            ctx.dependencies["llm.chat"],
            fetcher=fetcher,
            search=search,
            search_mode=mode,
        )
        return {"feature.conversation": self.service}

    async def start(self):
        pass

    async def stop(self):
        if self.search is not None:
            await self.search.close()


def _key(path) -> str:
    """r.jina.ai の API キー（任意）。読めなければ空。"""
    if not path:
        return ""

    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""
