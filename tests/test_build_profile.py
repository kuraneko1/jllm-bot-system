import unittest
from pathlib import Path

import yaml

from tools import build_profile


def backends():
    return {
        "backends": [
            {"id": "local", "kind": "voicevox", "display": "VOICE", "url": "http://127.0.0.1:50021"},
            {"id": "home", "kind": "irodori", "display": "HOME", "url": "http://127.0.0.1:8088",
             "voice": "clone a"},
            {"id": "drama3", "kind": "openai", "url": "http://127.0.0.1:8090", "voice": "v",
             "model": "m", "speed_mode": "atempo", "default_speed": 1.0,
             "ignore_speaker_speed": False, "seed": 42, "timeout": 120},
        ]
    }


def environment():
    return {
        "DISCORD_TOKEN": "SECRET-TOKEN",
        "IRODORI_API_KEY": "SECRET-KEY",
        "DATA_DIR": "/srv/data",
        "OWNER_ID": "1",
        "MEDIA_APPROVER_ID": "2",
        "TTS_SWITCH_USER_IDS": "3,4",
        "MESSAGE_CHANNEL_WHITELIST": "10,11",
        "GLOBAL_REACTION_WORDS": "v100,草",
        "DEFAULT_SPEED": "1.3",
        "DEFAULT_SPEAKER": "8",
        "DEFAULT_VOLUME": "0.3",
        "SAMPLE_VOLUME": "0.4",
        "READINGS_DASHBOARD_URL": "https://a.example,https://b.example:8443",
        "READINGS_DASHBOARD_PORT": "8765",
        "TTS_FAILOVER": "false",
        "TTS_HEALTH_SECONDS": "20",
        "TRANSLATION_BACKEND": "local",
        "TRANSLATION_API_URL": "http://127.0.0.1:8092",
        "SARASHINA_API_KEY_FILE": "/keys/sarashina",
        "SARASHINA_MODEL": "model-x",
        "SARASHINA_RPM": "60",
        "KITASAN_ITEM_CHARS": "5",
        "KITASAN_WORDS": "今北産業,きたさん",
        "ANIMA_URL": "http://127.0.0.1:8190",
        "ANIMA_MODEL": "anima",
        "DUCK_ENABLED": "true",
        "TALK_MODEL": "model-x",
    }


class BuildProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.summary = build_profile.build(
            environment(), backends(), Path("/srv/data")
        )
        self.plugins = self.profile["plugins"]

    def test_tts_instances_keep_legacy_ids_and_display_names(self):
        ids = self.plugins["tts.router"]["config"]["backend_ids"]
        self.assertEqual(ids, {
            "provider.tts.voicevox/local": "local",
            "provider.tts.irodori/home": "home",
            "provider.tts.openai/drama3": "drama3",
        })
        displays = self.plugins["tts.router"]["config"]["backend_displays"]
        self.assertEqual(displays["provider.tts.irodori/home"], "HOME")
        self.assertEqual(
            self.profile["bindings"]["tts.router"]["tts.provider"],
            ["provider.tts.voicevox/local", "provider.tts.irodori/home",
             "provider.tts.openai/drama3"],
        )
        self.assertEqual(self.plugins["provider.tts.openai"]["instances"]["drama3"]["seed"], 42)
        self.assertEqual(self.plugins["provider.tts.irodori"]["instances"]["home"]["data_dir"],
                         "/srv/data")

    def test_paths_are_absolute_and_dashboard_keeps_all_origins(self):
        config = self.plugins["feature.reader"]["config"]
        self.assertEqual(config["toggles_path"], "/srv/data/toggles.json")
        self.assertTrue(config["require_existing_toggles"])
        self.assertEqual(config["allowed_channel_ids"], [10, 11])
        self.assertEqual(self.plugins["web.dashboard"]["config"]["public_url"],
                         "https://a.example,https://b.example:8443")
        self.assertEqual(config["dashboard_url"], "https://a.example")
        self.assertTrue(self.plugins["web.dashboard"]["config"]["require_existing_passkey"])
        self.assertEqual(self.plugins["store.sessions"]["config"]["directory"],
                         "/srv/data/sessions")

    def test_values_are_coerced_and_bounded(self):
        self.assertFalse(self.plugins["tts.router"]["config"]["failover"])
        self.assertEqual(self.plugins["feature.kitasan"]["config"]["item_chars"], 20)
        self.assertEqual(self.plugins["feature.kitasan"]["config"]["words"],
                         ["今北産業", "きたさん"])
        self.assertTrue(self.plugins["speech.playback"]["config"]["duck_enabled"])
        self.assertEqual(self.plugins["feature.miq"]["config"]["layout"], "auto")

    def test_generated_profile_contains_no_secrets(self):
        written = yaml.safe_dump(self.profile, allow_unicode=True)
        for secret in ("SECRET-TOKEN", "SECRET-KEY"):
            self.assertNotIn(secret, written)
        self.assertIn("DISCORD_TOKEN", written)

    def test_eighteen_digit_ids_are_not_rounded(self):
        env = environment() | {"OWNER_ID": "612785552315645953"}
        profile, _ = build_profile.build(env, backends(), Path("/srv/data"))
        self.assertEqual(profile["plugins"]["feature.reader"]["config"]["owner_id"],
                         612785552315645953)
        self.assertEqual(profile["plugins"]["dictionary.media"]["config"]["owner_id"],
                         612785552315645953)
        self.assertEqual(profile["plugins"]["feature.reader"]["config"]["allowed_channel_ids"],
                         [10, 11])

    def test_effects_ir_falls_back_to_the_legacy_location(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            legacy = root / "services" / "ir" / "room.wav"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"RIFF")
            profile, _ = build_profile.build(environment(), backends(), data)
            config = profile["plugins"]["speech.effects"]["config"]
            self.assertEqual(config["ir_path"], str(legacy))
            self.assertEqual(profile["plugins"]["speech.playback"]["config"]["ir_path"],
                             str(legacy))
            own = data / "ir" / "room.wav"
            own.parent.mkdir()
            own.write_bytes(b"RIFF")
            profile, _ = build_profile.build(environment(), backends(), data)
            self.assertEqual(profile["plugins"]["speech.effects"]["config"]["ir_path"], str(own))

    def test_lifecycle_targets_come_from_the_environment(self):
        env = environment() | {
            "TTS_LIFECYCLE_TARGETS": '{"home": ["gpu", "irodori"], "local": null}',
            "TTS_LIFECYCLE_LOCAL_ONLY": "local",
            "TTS_LIFECYCLE_UNMANAGED": "external",
        }
        profile, _ = build_profile.build(env, backends(), Path("/srv/data"))
        config = profile["plugins"]["tts.lifecycle"]["config"]
        self.assertEqual(config["targets"], {"home": ["gpu", "irodori"], "local": None})
        self.assertEqual(config["local_only"], ["local"])
        self.assertEqual(config["unmanaged_hosts"], ["external"])

    def test_unknown_backend_kind_is_reported_not_silently_kept(self):
        broken = {"backends": [{"id": "x", "kind": "unknown", "url": "http://127.0.0.1:1"}]}
        profile, summary = build_profile.build(environment(), broken, Path("/srv/data"))
        self.assertEqual(summary["dropped_backends"], ["x"])
        self.assertEqual(profile["bindings"]["tts.router"]["tts.provider"], [])


if __name__ == "__main__":
    unittest.main()
