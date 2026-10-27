import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jllm.kernel import Manifest, Profile, ProfileError, discover, load_profile, resolve


class DiscoveryTests(unittest.TestCase):
    def test_discovery_reads_resource_without_loading_entry(self):
        raw = {
            "id": "example",
            "version": "1.0.0",
            "api": ">=1,<2",
            "entry": "must_not_import:Plugin",
        }
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "plugin.json").write_text(json.dumps(raw), encoding="utf-8")
            ep = SimpleNamespace(name="example", value="fake_pkg:plugin.json")
            with (
                patch("jllm.kernel.importlib.metadata.entry_points", return_value=[ep]),
                patch("jllm.kernel.importlib.resources.files", return_value=Path(temp)),
            ):
                self.assertEqual(discover()["example"].entry, "must_not_import:Plugin")

    def test_invalid_manifest_and_duplicate_ids_are_rejected(self):
        raw = {"id": "example", "version": "1.0.0", "api": ">=1,<2", "entry": "fake:Plugin"}
        with self.assertRaises(ProfileError):
            Manifest.from_dict({**raw, "api": ">=2,<3"})
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "plugin.json").write_text(json.dumps(raw), encoding="utf-8")
            eps = [SimpleNamespace(name=str(i), value="fake_pkg:plugin.json") for i in range(2)]
            with (
                patch("jllm.kernel.importlib.metadata.entry_points", return_value=eps),
                patch("jllm.kernel.importlib.resources.files", return_value=Path(temp)),
                self.assertRaises(ProfileError),
            ):
                discover()

    def test_profile_allowlist_does_not_activate_disabled_plugin(self):
        m = Manifest.from_dict(
            {"id": "example", "version": "1.0.0", "api": ">=1,<2", "entry": "fake:Plugin"}
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "profile.yaml")
            path.write_text("plugins:\n  example:\n    enabled: false\n", encoding="utf-8")
            p = resolve(load_profile(path), {"example": m})
            self.assertEqual(p.nodes, {})
        self.assertEqual(resolve(Profile({}), {"example": m}).nodes, {})
