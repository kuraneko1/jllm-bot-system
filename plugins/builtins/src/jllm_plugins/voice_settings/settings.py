import json
import os
from pathlib import Path

from .join_voice import JoinVoiceCache
from .lengths import VoiceLengths
from .speeds import VoiceSpeeds
from .volumes import VoiceVolumes


class JsonMap:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key, default=None):
        return self.entries.get(str(key), default)

    def set(self, key, value):
        self.entries[str(key)] = value
        self.save()

    def remove(self, key):
        if str(key) not in self.entries:
            return False
        del self.entries[str(key)]
        self.save()
        return True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class VoiceSettings:
    def __init__(self, storage, config):
        self.speeds = VoiceSpeeds(
            storage.legacy_path(config.get("speeds_path", "voice_speeds.json"))
        )
        self.volumes = VoiceVolumes(
            storage.legacy_path(config.get("volumes_path", "voice_volumes.json"))
        )
        self.lengths = VoiceLengths(
            storage.legacy_path(config.get("lengths_path", "voice_lengths.json"))
        )
        self.user_voices = JsonMap(
            storage.legacy_path(config.get("user_voices_path", "user_voices.json"))
        )
        self.forced_voices = JsonMap(
            storage.legacy_path(config.get("forced_voices_path", "forced_voices.json"))
        )
        for voice_map in (self.user_voices, self.forced_voices):
            voice_map.entries = {str(key): str(value) for key, value in voice_map.entries.items()}
        self.voice_entries = JsonMap(
            storage.legacy_path(config.get("announcement_history_path", "vc_announcements.json"))
        )
        self.join_cache = JoinVoiceCache(
            storage.legacy_path(config.get("announcement_cache_dir", "join_voice")),
            voice=config.get("announcement_voice", "clone こしあん"),
            limit=config.get("announcement_cache_limit", 500),
        )


class Plugin:
    async def setup(self, ctx):
        self.settings = VoiceSettings(ctx.dependencies["storage.local"], ctx.config)
        return {"voice.settings": self.settings}

    async def start(self):
        pass

    async def stop(self):
        pass
