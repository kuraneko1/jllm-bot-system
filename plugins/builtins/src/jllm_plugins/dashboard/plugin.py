from .dashboard import DashboardConfig, ReadingDashboard


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        config = DashboardConfig(
            d1_api_url=ctx.config.get("d1_api_url", "http://127.0.0.1:8083"),
            d1_timeout_seconds=ctx.config.get("d1_timeout_seconds", 20.0),
            duck_curve_path=ctx.config.get(
                "duck_curve_path", str(storage.legacy_path("duck_curve.json"))
            ),
            readings_dashboard_url=ctx.config.get("public_url", "http://127.0.0.1:8765"),
            talk_model=ctx.config.get("talk_model", "sarashina3-mini"),
            turtle_soup_max_questions=ctx.config.get("turtle_soup_max_questions", 100),
            turtle_soup_per_ip=ctx.config.get("turtle_soup_per_ip", 60),
            webchat_dir=ctx.config.get("webchat_dir", str(storage.legacy_path("webchat"))),
            webchat_max_text=ctx.config.get("webchat_max_text", 2000),
            webchat_per_ip=ctx.config.get("webchat_per_ip", 12),
        )
        self.dashboard = ReadingDashboard(
            ctx.dependencies["dictionary.reading"],
            ctx.dependencies["tts.synthesis"],
            reader=ctx.dependencies.get("feature.reader"),
            sessions=ctx.dependencies["store.sessions"],
            webchat_client=ctx.dependencies["llm.chat"],
            password_file=ctx.config.get(
                "auth_file", str(storage.legacy_path(".dashboard-auth.json"))
            ),
            host=ctx.config.get("host", "127.0.0.1"),
            port=ctx.config.get("port", 8765),
            public_url=config.readings_dashboard_url,
            require_existing_passkey=ctx.config.get("require_existing_passkey", False),
            effects=ctx.dependencies.get("speech.effects"),
            config=config,
        )
        ctx.scope.add(self.dashboard.close)
        return {"web.dashboard": self.dashboard}

    async def start(self):
        await self.dashboard.start()

    async def stop(self):
        pass
