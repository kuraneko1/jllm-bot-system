from .router import BackendSpec, TTSRouter


class Plugin:
    async def setup(self, ctx):
        providers = ctx.dependencies["tts.provider"]
        identifiers = ctx.dependency_ids["tts.provider"]
        specs = []
        for ident, backend in zip(identifiers, providers, strict=True):
            kind = getattr(backend, "name", "unknown") or "unknown"
            specs.append(
                BackendSpec(
                    id=ident,
                    kind=kind,
                    display=getattr(backend, "display_name", "") or kind,
                    options={"url": getattr(backend, "base_url", "")},
                )
            )
        storage = ctx.dependencies["storage.local"]
        self.router = TTSRouter(
            specs,
            providers=dict(zip(identifiers, providers, strict=True)),
            state_path=storage.legacy_path(ctx.config.get("state_file", "tts_state.json")),
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
