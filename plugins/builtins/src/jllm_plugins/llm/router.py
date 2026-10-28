from . import llm_backend
from .translation import SwitchableClient, build_translation_client


class RoutedSwitchableClient(SwitchableClient):
    async def switch(self, backend):
        result = await super().switch(backend)
        llm_backend.set_selection(backend)
        return result


class LLMState:
    snapshot = staticmethod(llm_backend.snapshot)
    stored = staticmethod(llm_backend.stored)
    backend_for = staticmethod(llm_backend.backend_for)
    active_backend = staticmethod(llm_backend.active_backend)
    active_read_mode = staticmethod(llm_backend.active_read_mode)
    set_selection = staticmethod(llm_backend.set_selection)
    set_channel = staticmethod(llm_backend.set_channel)
    clear_channel = staticmethod(llm_backend.clear_channel)


class Plugin:
    async def setup(self, ctx):
        backend = ctx.config.get("backend", "sarashina")
        state_file = ctx.dependencies["storage.local"].legacy_path(
            ctx.config.get("state_file", "llm_backend.json")
        )
        llm_backend.configure_state(state_file, backend)
        configs = {
            "local": dict(ctx.config.get("local", {})),
            "sarashina": dict(ctx.config.get("sarashina", {})),
            "atmos": dict(ctx.config.get("atmos", {})),
        }
        self.client = RoutedSwitchableClient(
            backend=backend,
            factory=lambda name: build_translation_client(name, configs.get(name, {})),
        )
        self.state = LLMState()
        ctx.scope.add(self.client.close)
        return {"llm.chat": self.client, "llm.read-mode": self.state}

    async def start(self):
        pass

    async def stop(self):
        pass
