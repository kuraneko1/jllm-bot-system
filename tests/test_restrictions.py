import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jllm_plugins.restrictions.plugin import Plugin as RestrictionsPlugin
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.storage.plugin import LocalStorage


class LocalStorageTests(unittest.TestCase):
    def test_paths_are_scoped_and_json_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = LocalStorage(temp)
            path = storage.legacy_path("nested", "value.json")
            storage.write_json(path, {"値": 1})
            self.assertEqual(json.loads(path.read_text()), {"値": 1})
            self.assertFalse(path.with_suffix(".tmp").exists())
            self.assertEqual(
                storage.plugin_path("feature.test", "state.json"),
                Path(temp).resolve() / "plugins" / "feature.test" / "state.json",
            )
            with self.assertRaises(ValueError):
                storage.legacy_path("..", "outside.json")


class UserRestrictionsTests(unittest.TestCase):
    def test_legacy_load_and_queries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "user_restrictions.json")
            path.write_text(
                json.dumps(
                    {
                        "1:20": ["gif", "unknown", "wav"],
                        "1:21": [],
                        "broken": "wav",
                        "2:20": ["ignore"],
                    }
                ),
                encoding="utf-8",
            )
            store = UserRestrictions(path)
            self.assertEqual(store.flags(1, 20), frozenset({"wav", "gif"}))
            self.assertTrue(store.blocked(1, 20, "gif"))
            self.assertTrue(store.ignored(2, 20))
            self.assertFalse(store.ignored(None, 20))
            self.assertEqual(store.guild_entries(1), {20: ["wav", "gif"]})
            self.assertEqual(store.guild_entries(2), {20: ["ignore"]})

    def test_set_clear_and_reload_preserves_legacy_format(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "user_restrictions.json")
            store = UserRestrictions(path)
            self.assertTrue(store.set_flag(2, 20, "ignore", True))
            self.assertFalse(store.set_flag(2, 20, "ignore", True))
            store.set_flag(2, 20, "wav", True)
            store.set_flag(1, 10, "gif", True)
            self.assertEqual(
                json.loads(path.read_text()),
                {"1:10": ["gif"], "2:20": ["wav", "ignore"]},
            )
            self.assertTrue(store.ignored(2, 20))
            self.assertTrue(store.clear(2, 20))
            self.assertFalse(store.clear(2, 20))
            self.assertEqual(UserRestrictions(path).guild_entries(2), {})
            with self.assertRaises(ValueError):
                store.set_flag(1, 10, "unknown", True)

    def test_broken_file_and_no_path_are_noops(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "broken.json")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(UserRestrictions(path).entries, {})
        store = UserRestrictions()
        store.set_flag(1, 2, "wav", True)
        store.save()

    def test_plugin_receives_storage_by_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = RestrictionsPlugin()
            ctx = SimpleNamespace(
                config={"path": "custom.json"},
                dependencies={"storage.local": LocalStorage(temp)},
            )
            import asyncio

            services = asyncio.run(plugin.setup(ctx))
            self.assertIs(services["policy.restrictions"], plugin.restrictions)
            self.assertEqual(plugin.restrictions.path, Path(temp).resolve() / "custom.json")


if __name__ == "__main__":
    unittest.main()
