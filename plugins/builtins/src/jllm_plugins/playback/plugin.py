import asyncio

from .player import PlayerConfig, SpeechPlayer


class SpeechPlayerFactory:
    def __init__(self, config: PlayerConfig, effects):
        self.config = config
        self.effects = effects
        self.players: set[SpeechPlayer] = set()

    def create(self, voice_client, tts, text_channel_id: int) -> SpeechPlayer:
        player = SpeechPlayer(
            voice_client,
            tts,
            text_channel_id,
            config=self.config,
            effects=self.effects,
        )
        self.players.add(player)
        return player

    async def close(self):
        await asyncio.gather(*(player.close() for player in self.players), return_exceptions=True)


class Plugin:
    async def setup(self, ctx):
        config = PlayerConfig(
            speaker_id=ctx.config.get("speaker_id", 1),
            speed=ctx.config.get("speed", 1.2),
            volume=ctx.config.get("volume", 0.2),
            sample_volume=ctx.config.get("sample_volume", 0.2),
            allow_nonverbal_emojis=ctx.config.get("allow_nonverbal_emojis", False),
            max_queue_size=ctx.config.get("max_queue_size", 30),
            tail_silence_ms=ctx.config.get("tail_silence_ms", 200),
            ir_path=ctx.config.get("ir_path", "data/ir/room.wav"),
            duck_enabled=ctx.config.get("duck_enabled", False),
            duck_curve_path=ctx.config.get("duck_curve_path", "data/duck_curve.json"),
            duck_level=ctx.config.get("duck_level", 0.2),
            duck_hold_seconds=ctx.config.get("duck_hold_seconds", 0.8),
            duck_tick_seconds=ctx.config.get("duck_tick_seconds", 0.04),
            duck_trigger_seconds=ctx.config.get("duck_trigger_seconds", 1.0),
            duck_debug=ctx.config.get("duck_debug", False),
        )
        self.factory = SpeechPlayerFactory(config, ctx.dependencies["speech.effects"])
        ctx.scope.add(self.factory.close)
        return {"speech.playback": self.factory}

    async def start(self):
        pass

    async def stop(self):
        pass
