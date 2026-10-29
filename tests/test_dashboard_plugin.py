import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.llm import llm_backend
from webauthn.helpers import bytes_to_base64url

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "dashboard.yaml"


class DashboardPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_starts_and_stops_http_server(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                node = plan.nodes["web.dashboard/main"]
                self.assertEqual(node.state, "RUNNING")
                dashboard = node.services["web.dashboard"]
                self.assertIsNotNone(dashboard._runner)
                self.assertIsNotNone(dashboard.webchat)
            finally:
                await kernel.stop()
            llm_backend._reset_config()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))

    async def test_custom_legacy_paths_and_existing_passkey(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "legacy-auth.json"
            passkeys = auth.with_suffix(".passkeys.json")
            passkeys.write_text(json.dumps({
                "user_id": bytes_to_base64url(bytes(32)),
                "credentials": [{"id": bytes_to_base64url(b"id"),
                                 "public_key": bytes_to_base64url(b"key"),
                                 "sign_count": 0, "name": "test"}],
            }), encoding="utf-8")
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = str(root / "data")
            profile.plugins["web.dashboard"]["critical"] = True
            profile.plugins["web.dashboard"]["config"].update({
                "require_existing_passkey": True,
                "auth_file": str(auth),
                "webchat_dir": str(root / "legacy-webchat"),
                "duck_curve_path": str(root / "legacy-duck.json"),
            })
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                dashboard = plan.nodes["web.dashboard/main"].services["web.dashboard"]
                self.assertEqual(dashboard._passkey_path, passkeys)
                self.assertEqual(dashboard.webchat.directory, root / "legacy-webchat")
                self.assertEqual(dashboard.config.duck_curve_path, str(root / "legacy-duck.json"))
            finally:
                await kernel.stop()
                llm_backend._reset_config()

    async def test_production_mode_fails_closed_without_existing_passkey(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["web.dashboard"]["critical"] = True
            profile.plugins["web.dashboard"]["config"]["require_existing_passkey"] = True
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "critical plugin failed: web.dashboard/main"
                ):
                    await kernel.start()
                self.assertIsNone(plan.nodes["web.dashboard/main"].services.get("web.dashboard"))
                self.assertFalse((Path(temp) / "webchat").exists())
            finally:
                await kernel.stop()
                llm_backend._reset_config()


if __name__ == "__main__":
    unittest.main()
