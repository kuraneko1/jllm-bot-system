from . import llm_backend
from .admin_config import DEFAULTS, AdminConfig
from .translation import SwitchableClient, build_translation_client


class RoutedSwitchableClient(SwitchableClient):
    async def switch(self, backend):
        if backend not in llm_backend.BACKENDS:
            raise ValueError("不明なバックエンドです。")
        result = await super().switch(backend)
        if llm_backend.stored()["backend"] != backend:
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
        configs = {name: dict(ctx.config.get(name, {})) for name in DEFAULTS}
        admin = AdminConfig(
            ctx.dependencies["storage.local"].legacy_path("llm_provider_config.json"), configs
        )
        self.client = RoutedSwitchableClient(
            backend=llm_backend.snapshot()["global_backend"],
            factory=lambda name: build_translation_client(name, admin.config(name)),
        )
        self.client._admin = admin
        self.client._admin_factory = lambda name, config: build_translation_client(name, config)
        self.state = LLMState()
        ctx.scope.add(self.client.close)
        return {"llm.chat": self.client, "llm.read-mode": self.state}

    async def start(self):
        pass

    async def stop(self):
        pass
