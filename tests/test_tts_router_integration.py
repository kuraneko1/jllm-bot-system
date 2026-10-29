import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.tts_router.plugin import Plugin
from jllm_tts import TTSBackendError

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-router.yaml"


class TTSRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_providers_bind_in_profile_order(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["provider.tts.irodori"]["instances"]["home"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            with patch(
                "jllm_plugins.tts_router.plugin.TTSRouter.probe",
                new=AsyncMock(return_value=True),
            ):
                try:
                    await kernel.start()
                    router = plan.nodes["tts.router/main"].services["tts.synthesis"]
                    self.assertEqual(router.active, "provider.tts.irodori/home")
                    self.assertEqual(router.preferred, "provider.tts.irodori/home")
                    self.assertEqual(
                        [spec.id for spec in router.specs],
                        [
                            "provider.tts.irodori/home",
                            "provider.tts.speechcpp/local",
                            "provider.tts.openai/drama3",
                            "provider.tts.voicevox/local",
                        ],
                    )
                    self.assertTrue(router.failover)
                    self.assertEqual(
                        plan.nodes["tts.router/main"].state,
                        "RUNNING",
                    )
                finally:
                    await kernel.stop()
            for backend in router.backends.values():
                self.assertTrue(backend.session.closed)
            self.assertTrue((Path(temp) / "tts_state.json").is_file())

    async def test_legacy_ids_survive_start_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "tts_state.json"
            state.write_text(json.dumps({
                "preferred": "drama3", "active": "home",
                "streaming": {"drama3": False}, "failover": False,
            }), encoding="utf-8")
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["provider.tts.irodori"]["instances"]["home"]["data_dir"] = temp
            profile.plugins["tts.router"]["config"]["backend_displays"] = {
                "provider.tts.irodori/home": "自宅GPU",
                "provider.tts.openai/drama3": "予備の音声",
            }
            profile.plugins["tts.router"]["config"]["require_existing_state"] = True
            profile.plugins["tts.router"]["config"]["backend_ids"] = {
                "provider.tts.irodori/home": "home",
                "provider.tts.speechcpp/local": "spare",
                "provider.tts.openai/drama3": "drama3",
                "provider.tts.voicevox/local": "local",
            }
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            with patch("jllm_plugins.tts_router.plugin.TTSRouter.probe",
                       new=AsyncMock(return_value=True)):
                try:
                    await kernel.start()
                    router = plan.nodes["tts.router/main"].services["tts.synthesis"]
                    self.assertEqual((router.preferred, router.active), ("drama3", "home"))
                    self.assertEqual(router.spec_of("home").label(), "自宅GPU")
                    self.assertEqual(router.spec_of("drama3").label(), "予備の音声")
                    self.assertEqual(router.status()["backends"][0]["display"], "自宅GPU")
                    self.assertFalse(router.streaming["drama3"])
                    self.assertFalse(router.failover)
                    await router.set_active("spare")
                finally:
                    await kernel.stop()
            saved = json.loads(state.read_text(encoding="utf-8"))
            # 旧版は backends のIDだけを認識する。全保存IDが旧IDのまま。
            self.assertEqual((saved["preferred"], saved["active"]), ("spare", "spare"))
            self.assertEqual(saved["streaming"], {"drama3": False})
            self.assertFalse(saved["failover"])
            self.assertTrue(set(saved["unhealthy"]) <= {"home", "spare", "drama3", "local"})

    async def test_required_state_fails_closed_before_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "tts_state.json"
            ctx = SimpleNamespace(
                dependencies={
                    "tts.provider": [Mock()],
                    "storage.local": SimpleNamespace(legacy_path=lambda name: Path(temp) / name),
                },
                dependency_ids={"tts.provider": ["provider/a"]},
                config={"backend_ids": {"provider/a": "home"}, "require_existing_state": True},
                scope=Mock(),
            )
            for content in (None, "", "{}", "[]", "{broken",
                            '{"preferred":"missing"}', '{"active":"missing"}',
                            '{"streaming":{"missing":false}}',
                            '{"unhealthy":{"missing":1}}', '{"unhealthy":[]}',
                            '{"unrelated":true}', '{"preferred":null}',
                            '{"active":null}', '{"failover":"false"}',
                            '{"streaming":{}}', '{"unhealthy":{"home":1}}',
                            '{"preferred":"home","failover":null}',
                            '{"preferred":"home","streaming":{"home":"false"}}',
                            '{"preferred":"home","unhealthy":{"home":"bad"}}'):
                with self.subTest(content=content):
                    if state.exists():
                        state.unlink()
                    if content is not None:
                        state.write_text(content, encoding="utf-8")
                    with self.assertRaises(TTSBackendError):
                        await Plugin().setup(ctx)
                    self.assertEqual(state.exists(), content is not None)
                    if content is not None:
                        self.assertEqual(state.read_text(encoding="utf-8"), content)
                    names = await asyncio.to_thread(
                        lambda: sorted(p.name for p in Path(temp).iterdir())
                    )
                    self.assertEqual(names, ["tts_state.json"] if content is not None else [])
                    ctx.scope.add.assert_not_called()

            # 旧版で可能な部分状態は、意味のある設定があれば受け入れる。
            for content in ('{"preferred":"home"}', '{"active":"home"}',
                            '{"failover":false}', '{"streaming":{"home":false}}',
                            '{"preferred":"home","streaming":{}}'):
                state.write_text(content, encoding="utf-8")
                plugin = Plugin()
                result = await plugin.setup(ctx)
                self.assertEqual(result["tts.synthesis"].preferred, "home")
                self.assertEqual(state.read_text(encoding="utf-8"), content)

            # 必須モードを使わなければ、これまでの部分・空状態の扱いを変えない。
            ctx.config["require_existing_state"] = False
            for content in ("{}", '{"unrelated":true}', '{"preferred":null}',
                            '{"failover":"false"}', '{"streaming":{"home":"false"}}'):
                state.write_text(content, encoding="utf-8")
                await Plugin().setup(ctx)
                self.assertEqual(state.read_text(encoding="utf-8"), content)

    async def test_display_mapping_rejects_unknown_and_blank_values(self):
        for mapping in ({"provider/missing": "表示"}, {"provider/a": "  "}):
            ctx = SimpleNamespace(
                dependencies={"tts.provider": [Mock()]},
                dependency_ids={"tts.provider": ["provider/a"]},
                config={"backend_displays": mapping},
            )
            with self.subTest(mapping=mapping), self.assertRaises(TTSBackendError):
                await Plugin().setup(ctx)

    async def test_mapping_must_cover_all_providers_and_be_unique(self):
        for mapping in ({"provider/a": "old"},
                        {"provider/a": "old", "provider/b": "old"},
                        {"provider/a": "provider/b", "provider/b": "other"}):
            ctx = SimpleNamespace(
                dependencies={"tts.provider": [Mock(), Mock()]},
                dependency_ids={"tts.provider": ["provider/a", "provider/b"]},
                config={"backend_ids": mapping},
            )
            with self.subTest(mapping=mapping), self.assertRaises(TTSBackendError):
                await Plugin().setup(ctx)


if __name__ == "__main__":
    unittest.main()
