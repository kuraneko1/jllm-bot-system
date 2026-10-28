from jllm_plugins.llm.translation import TranslationSettings

from .cog import TranslationCog, TranslationCogConfig


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        config = TranslationCogConfig(
            allowed_channel_ids=frozenset(ctx.config.get("allowed_channel_ids", [])),
            owner_id=ctx.config.get("owner_id", 0),
            session_dir=str(storage.legacy_path("sessions")),
            talk_call_suffix=ctx.config.get("talk_call_suffix", ""),
            talk_log_path=str(storage.legacy_path("talk_log.jsonl")),
            talk_mention_gate=ctx.config.get("talk_mention_gate", "open"),
            talk_model=ctx.config.get("talk_model", ""),
            talk_open_channels=frozenset(ctx.config.get("talk_open_channels", [])),
            talk_session_seconds=ctx.config.get("talk_session_seconds", 1800.0),
            talk_stats_path=str(storage.legacy_path("talk_stats.json")),
            talk_system_prompt_file=ctx.config.get("talk_system_prompt_file", ""),
            translation_settings_path=str(storage.legacy_path("translation.json")),
            tts_switch_user_ids=frozenset(ctx.config.get("tts_switch_user_ids", [])),
        )
        self.cog = TranslationCog(
            ctx.dependencies["discord.gateway"].bot,
            settings=TranslationSettings(config.translation_settings_path),
            client=ctx.dependencies["llm.chat"],
            controller=ctx.dependencies.get("llm.controller"),
            talk_log=ctx.dependencies["feature.talk-log"],
            sessions=ctx.dependencies["store.sessions"],
            restrictions=ctx.dependencies["policy.restrictions"],
            conversation=ctx.dependencies.get("feature.conversation"),
            kitasan=ctx.dependencies.get("feature.kitasan"),
            config=config,
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.cog_unload()
