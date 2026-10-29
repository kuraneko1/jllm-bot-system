from jllm_tts import TTSBackendError

from .router import BackendSpec, TTSRouter


class Plugin:
    async def setup(self, ctx):
        providers = ctx.dependencies["tts.provider"]
        identifiers = ctx.dependency_ids["tts.provider"]
        aliases = ctx.config.get("backend_ids", {})
        if "backend_ids" in ctx.config and (
            not aliases
            or set(aliases) != set(identifiers)
            or len(set(aliases.values())) != len(aliases)
            or any(not isinstance(value, str) or not value.strip() or value != value.strip()
                   for value in aliases.values())
            or any(value in identifiers and value != key for key, value in aliases.items())
        ):
            raise TTSBackendError("backend_ids は全 provider の一意な旧IDを指定してください。")
        displays = ctx.config.get("backend_displays", {})
        if set(displays) - set(identifiers) or any(
            not isinstance(value, str) or not value.strip() for value in displays.values()
        ):
            raise TTSBackendError(
                "backend_displays は接続済み provider の表示名を指定してください。"
            )
        specs = []
        for ident, backend in zip(identifiers, providers, strict=True):
            kind = getattr(backend, "name", "unknown") or "unknown"
            specs.append(
                BackendSpec(
                    id=aliases.get(ident, ident),
                    kind=kind,
                    display=displays.get(ident) or getattr(backend, "display_name", "") or kind,
                    options={"url": getattr(backend, "base_url", "")},
                )
            )
        storage = ctx.dependencies["storage.local"]
        self.router = TTSRouter(
            specs,
            providers={aliases.get(ident, ident): backend
                       for ident, backend in zip(identifiers, providers, strict=True)},
            id_aliases=aliases,
            state_path=storage.legacy_path(ctx.config.get("state_file", "tts_state.json")),
            require_existing_state=ctx.config.get("require_existing_state", False),
            disabled_voices_path=storage.legacy_path(
                ctx.config.get("disabled_voices_path", "disabled_voices.json")
            ),
            failover=ctx.config.get("failover", True),
            health_seconds=ctx.config.get("health_seconds", 30.0),
            lifecycle=ctx.dependencies.get("tts.lifecycle"),
        )
        ctx.scope.add(self.router.close)
        return {"tts.synthesis": self.router}

    async def start(self):
        await self.router.setup()

    async def stop(self):
        pass
