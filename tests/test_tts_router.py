"""TTSバックエンドの多重化（切替・フォールバック・自動復帰）。"""

import json
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.tts_router.router import BackendSpec, TTSRouter, load_specs
from jllm_tts import TTSBackend, TTSBackendError, TTSContentError, Voice


class FakeBackend(TTSBackend):
    """テスト用のバックエンド。``fail`` を立てると接続できない状態を真似る。"""

    supports_voice_management = True

    def __init__(self, ident, *, fail=False, voice="既定"):
        self.id = ident
        self.fail = fail
        self.voice = voice
        self.calls = []
        self.closed = False
        self.disabled_voices = set()

    def describe(self):
        return f"fake({self.id})"

    async def list_voices(self):
        self.calls.append("list_voices")

        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

        return [Voice(id=self.id, name=self.voice)]

    async def synthesize(self, text, speaker_id, speed=1.0):
        self.calls.append(("synthesize", text, speaker_id, speed))

        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

        return f"wav:{self.id}:{text}".encode()

    async def health(self):
        self.calls.append("health")

        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

    async def voice_library(self):
        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

        return [{"name": self.voice, "enabled": True, "deletable": True}]

    async def add_voice(self, name, audio):
        return {"name": name}

    async def delete_voice(self, name):
        return {"deleted": name}

    async def set_voice_enabled(self, name, enabled):
        return {"name": name, "enabled": enabled}

    async def close(self):
        self.closed = True


def specs(*items) -> list[BackendSpec]:
    return [
        BackendSpec(id=ident, kind="fake", display=label, options={"url": f"http://{ident}"})
        for ident, label in items
    ]


class TTSRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "tts_state.json"
        self.backends: dict[str, FakeBackend] = {}

    def make_router(self, first_fails=False, second_fails=False, failover=True, preferred=None):
        self.backends = {
            "home": FakeBackend("home", fail=first_fails, voice="こしあん"),
            "spare": FakeBackend("spare", fail=second_fails, voice="あんこ"),
        }
        state = {"preferred": preferred} if preferred else {}
        self.state.write_text(json.dumps(state), encoding="utf-8")

        return TTSRouter(
            specs(("home", "自宅"), ("spare", "予備")),
            env={},
            state_path=self.state,
            failover=failover,
            health_seconds=30,
            builder=lambda spec: self.backends[spec.id],
        )

    async def test_uses_the_preferred_backend_first(self):
        router = self.make_router()

        self.assertEqual(router.active, "home")
        self.assertEqual(
            await router.synthesize("こんにちは", "こしあん"), "wav:home:こんにちは".encode()
        )
        self.assertEqual([v.name for v in await router.list_voices()], ["こしあん"])
        self.assertIn("自宅", router.describe())
        self.assertEqual(self.backends["spare"].calls, [])

    async def test_falls_back_when_the_active_backend_is_down(self):
        router = self.make_router(first_fails=True)

        audio = await router.synthesize("やあ", "こしあん")

        self.assertEqual(audio, "wav:spare:やあ".encode())
        self.assertEqual(router.active, "spare")
        self.assertEqual(router.preferred, "home")
        self.assertIn("退避中", router.describe())
        self.assertIn("home", router.unhealthy)
        # 状態はディスクにも残る（再起動しても退避先を覚えている）
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["active"], "spare")

    async def test_returns_to_the_preferred_backend_when_it_recovers(self):
        router = self.make_router(first_fails=True)
        await router.synthesize("やあ", "こしあん")
        self.assertEqual(router.active, "spare")

        self.backends["home"].fail = False
        status = await router.check_all()

        self.assertEqual(router.active, "home")
        self.assertTrue(all(item["healthy"] for item in status["backends"]))

    async def test_manual_switch_changes_the_preferred_backend(self):
        router = self.make_router()

        result = await router.set_active("spare")

        self.assertEqual(result["active"], "spare")
        self.assertEqual(router.preferred, "spare")
        self.assertEqual(await router.synthesize("やあ", "あんこ"), "wav:spare:やあ".encode())
        # 優先も spare になったので、home が復帰しても戻らない
        await router.check_all()
        self.assertEqual(router.active, "spare")

    async def test_unknown_backend_is_rejected(self):
        router = self.make_router()

        with self.assertRaises(TTSBackendError):
            await router.set_active("nowhere")

    async def test_failover_can_be_disabled(self):
        router = self.make_router(first_fails=True, failover=False)

        with self.assertRaises(TTSBackendError):
            await router.synthesize("やあ", "こしあん")

        self.assertEqual(router.active, "home")
        self.assertEqual(self.backends["spare"].calls, [])

    async def test_all_backends_down_reports_both_errors(self):
        router = self.make_router(first_fails=True, second_fails=True)

        with self.assertRaises(TTSBackendError) as caught:
            await router.list_voices()

        self.assertIn("home", str(caught.exception))
        self.assertIn("spare", str(caught.exception))

    async def test_status_lists_every_backend(self):
        router = self.make_router(first_fails=True)
        await router.synthesize("やあ", "こしあん")

        status = router.status()

        self.assertEqual(status["preferred"], "home")
        self.assertEqual(status["active"], "spare")
        self.assertTrue(status["failover"])
        self.assertEqual([item["id"] for item in status["backends"]], ["home", "spare"])
        self.assertFalse(status["backends"][0]["healthy"])
        self.assertTrue(status["backends"][1]["healthy"])
        self.assertIn("接続できません", status["backends"][0]["error"])
        self.assertEqual(status["backends"][0]["url"], "http://home")

    async def test_voice_management_goes_through_the_active_backend(self):
        router = self.make_router()

        self.assertTrue(router.supports_voice_management)
        self.assertEqual(
            await router.voice_library(), [{"name": "こしあん", "enabled": True, "deletable": True}]
        )
        self.assertEqual(await router.add_voice("new", b"wav"), {"name": "new"})
        self.assertEqual(
            await router.set_voice_enabled("new", False), {"name": "new", "enabled": False}
        )
        self.assertEqual(await router.delete_voice("new"), {"deleted": "new"})

    async def test_disabled_voices_are_shared_across_backends(self):
        """無効化した話者は1ファイル（disabled_voices.json）が正で、エンジン共通。"""

        disabled = self.root / "disabled_voices.json"
        disabled.write_text(json.dumps(["こしあん"], ensure_ascii=False), encoding="utf-8")

        router = self.make_router()
        router.env = {"VOICE_DISABLED_PATH": str(disabled)}

        self.assertEqual(router.disabled_voices, {"こしあん"})

        # active なバックエンドが無効集合を持たなくても（DRAMA3 等）同じ結果になる
        router.active = "spare"
        self.assertEqual(router.disabled_voices, {"こしあん"})

        # バックエンド側が持つ無効集合は和集合で足す
        self.backends["spare"].disabled_voices = {"わかもと"}
        self.assertEqual(router.disabled_voices, {"こしあん", "わかもと"})

    async def test_voice_catalog_lists_every_engine(self):
        """話者クローンページ用に、全エンジンの話者をまとめて返す（1つ落ちても他は返す）。"""

        disabled = self.root / "disabled_voices.json"
        disabled.write_text(json.dumps(["あんこ"], ensure_ascii=False), encoding="utf-8")

        router = self.make_router(first_fails=True)
        router.env = {"VOICE_DISABLED_PATH": str(disabled)}

        catalog = await router.voice_catalog()

        self.assertEqual([entry["id"] for entry in catalog], ["home", "spare"])
        self.assertTrue(catalog[0]["active"])
        self.assertTrue(catalog[0]["manageable"])
        self.assertIn("接続できません", catalog[0]["error"])
        self.assertEqual(catalog[0]["voices"], [])
        self.assertFalse(catalog[1]["active"])
        self.assertEqual(
            catalog[1]["voices"],
            [
                {"name": "あんこ", "id": "spare", "enabled": False},
            ],
        )

    async def test_catalog_toggle_writes_the_shared_file_for_any_engine(self):
        """どのエンジンの話者でも、名前だけで共通ファイルを書き換える（和集合で効く）。"""

        disabled = self.root / "disabled_voices.json"
        router = self.make_router()
        router.env = {"VOICE_DISABLED_PATH": str(disabled)}

        router.set_shared_voice_enabled("こしあん", False)
        self.assertEqual(json.loads(disabled.read_text("utf-8")), ["こしあん"])
        self.assertIn("こしあん", router.disabled_voices)
        # 読み込み済みのバックエンドにも即反映する（再起動なしでIrodoriの一覧も変わる）
        self.assertIn("こしあん", self.backends["home"].disabled_voices)

        router.set_shared_voice_enabled("こしあん", True)
        self.assertEqual(json.loads(disabled.read_text("utf-8")), [])
        self.assertNotIn("こしあん", router.disabled_voices)
        self.assertNotIn("こしあん", self.backends["home"].disabled_voices)

    async def test_standard_none_voice_can_not_be_disabled(self):
        router = self.make_router()
        router.env = {"VOICE_DISABLED_PATH": str(self.root / "disabled_voices.json")}

        with self.assertRaises(TTSBackendError):
            router.set_shared_voice_enabled("none", False)

    async def test_missing_disabled_store_is_ignored(self):
        router = self.make_router()
        router.env = {"VOICE_DISABLED_PATH": str(self.root / "nowhere.json")}

        self.assertEqual(router.disabled_voices, set())

    async def test_close_stops_watching_and_closes_every_backend(self):
        router = self.make_router()
        router.start_watch()
        self.assertIsNotNone(router._task)

        await router.close()

        self.assertTrue(all(backend.closed for backend in self.backends.values()))
        self.assertIsNone(router._task)

    async def test_setup_switches_away_when_the_preferred_is_down_at_startup(self):
        router = self.make_router(first_fails=True)

        await router.setup()

        self.assertEqual(router.active, "spare")
        await router.close()


class StreamingFake(FakeBackend):
    """``stream()`` を持つバックエンド（drama3 の openai 相当）。"""

    supports_streaming = True

    def __init__(self, ident, *, content_fail=False, ignore_speed=False, **kwargs):
        super().__init__(ident, **kwargs)
        self.content_fail = content_fail
        self.ignore_speaker_speed = ignore_speed

    async def synthesize(self, text, speaker_id, speed=1.0):
        if self.content_fail:
            self.calls.append(("synthesize", text, speaker_id, speed))
            raise TTSContentError("drama3-small ended before producing audio")

        return await super().synthesize(text, speaker_id, speed)

    async def stream(self, text, speaker_id, speed=1.0):
        self.calls.append(("stream", text))
        yield f"pcm:{self.id}:".encode()
        yield text.encode()


class TTSRouterStreamingTests(unittest.IsolatedAsyncioTestCase):
    """ルーター越しのストリーミング・配信方式・空テイクの扱い。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "tts_state.json"

    def make_router(self, backends, *, preferred=None, state=None):
        self.backends = backends
        payload = dict(state or {})

        if preferred:
            payload["preferred"] = preferred

        self.state.write_text(json.dumps(payload), encoding="utf-8")

        return TTSRouter(
            specs(*[(ident, ident) for ident in backends]),
            env={},
            state_path=self.state,
            builder=lambda spec: self.backends[spec.id],
        )

    async def test_streams_through_the_active_backend(self):
        router = self.make_router({"drama3": StreamingFake("drama3"), "z170": FakeBackend("z170")})

        self.assertTrue(router.supports_streaming)
        chunks = [chunk async for chunk in router.stream("こんにちは", "test")]

        self.assertEqual(b"".join(chunks), "pcm:drama3:こんにちは".encode())

    async def test_a_backend_without_stream_is_not_streamed(self):
        router = self.make_router({"z170": FakeBackend("z170"), "drama3": StreamingFake("drama3")})

        self.assertFalse(router.supports_streaming)

        with self.assertRaises(TTSBackendError):
            [chunk async for chunk in router.stream("あ", "test")]

    async def test_qualified_ids_are_converted_to_legacy_ids_when_saved(self):
        self.state.write_text(json.dumps({
            "preferred": "provider/openai", "active": "provider/irodori",
            "streaming": {"provider/openai": False}, "failover": False,
        }), encoding="utf-8")
        backends = {"drama3": StreamingFake("drama3"), "home": FakeBackend("home")}
        router = TTSRouter(
            specs(("drama3", "DRAMA3"), ("home", "Home")),
            providers=backends, state_path=self.state,
            id_aliases={"provider/openai": "drama3", "provider/irodori": "home"},
        )
        self.assertEqual((router.preferred, router.active), ("drama3", "home"))
        self.assertFalse(router.streaming["drama3"])
        router.set_failover(True)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["preferred"], "drama3")
        self.assertEqual(saved["active"], "home")
        self.assertEqual(saved["streaming"], {"drama3": False})
        # 同じID構成の旧方式ルーターでも変更後の状態を読める。
        old_style = TTSRouter(specs(("drama3", "DRAMA3"), ("home", "Home")),
                              providers=backends, state_path=self.state)
        self.assertEqual((old_style.preferred, old_style.active), ("drama3", "home"))
        self.assertFalse(old_style.streaming["drama3"])

    async def test_unknown_or_conflicting_state_is_not_overwritten(self):
        for payload in (
            {"preferred": "missing"},
            {"streaming": {"provider/openai": True, "drama3": False}},
            {"streaming": {"missing": False}},
        ):
            original = json.dumps(payload)
            self.state.write_text(original, encoding="utf-8")
            with self.subTest(payload=payload), self.assertRaises(TTSBackendError):
                TTSRouter(specs(("drama3", "DRAMA3")),
                          providers={"drama3": StreamingFake("drama3")},
                          state_path=self.state, id_aliases={"provider/openai": "drama3"})
            self.assertEqual(self.state.read_text(encoding="utf-8"), original)
        self.state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(TTSBackendError):
            TTSRouter(specs(("drama3", "DRAMA3")),
                      providers={"drama3": StreamingFake("drama3")}, state_path=self.state)
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{broken")

    async def test_stream_mode_can_be_set_to_wav_and_is_remembered(self):
        router = self.make_router({"drama3": StreamingFake("drama3"), "z170": FakeBackend("z170")})

        router.set_streaming("drama3", False)

        self.assertFalse(router.supports_streaming)
        self.assertEqual(router.status()["backends"][0]["stream_mode"], "wav")
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["streaming"], {"drama3": False})

        reopened = self.make_router(
            {"drama3": StreamingFake("drama3"), "z170": FakeBackend("z170")},
            state=saved,
        )
        self.assertFalse(reopened.supports_streaming)

    async def test_stream_mode_cannot_be_enabled_on_a_wav_only_backend(self):
        router = self.make_router({"z170": FakeBackend("z170"), "drama3": StreamingFake("drama3")})

        with self.assertRaises(TTSBackendError):
            router.set_streaming("z170", True)

        self.assertFalse(router.status()["backends"][0]["stream_capable"])

    async def test_failover_toggle_overrides_the_config_and_is_remembered(self):
        router = self.make_router({"home": FakeBackend("home"), "spare": FakeBackend("spare")})

        router.set_failover(False)

        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIs(saved["failover"], False)
        reopened = self.make_router(
            {"home": FakeBackend("home"), "spare": FakeBackend("spare")}, state=saved
        )
        self.assertFalse(reopened.failover)

    async def test_an_empty_take_is_read_elsewhere_without_switching(self):
        router = self.make_router(
            {
                "drama3": StreamingFake("drama3", content_fail=True),
                "z170": FakeBackend("z170"),
            }
        )

        audio = await router.synthesize("あ", "test")

        self.assertEqual(audio, b"wav:z170:\xe3\x81\x82")
        self.assertEqual(router.active, "drama3")
        self.assertNotIn("drama3", router.unhealthy)

    async def test_known_down_backends_are_tried_last(self):
        router = self.make_router(
            {
                "home": FakeBackend("home", fail=True),
                "dead": FakeBackend("dead", fail=True),
                "spare": FakeBackend("spare"),
            }
        )
        router.unhealthy["dead"] = 1.0

        await router.synthesize("あ", 1)

        self.assertEqual(self.backends["dead"].calls, [])
        self.assertEqual(router.active, "spare")

    async def test_speed_policy_follows_the_active_backend(self):
        router = self.make_router(
            {"drama3": StreamingFake("drama3", ignore_speed=True), "z170": FakeBackend("z170")}
        )

        self.assertEqual(router.resolve_speed(1.4), 1.0)

        await router.set_active("z170")
        self.assertEqual(router.resolve_speed(1.4), 1.4)


class LoadSpecsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tts-backends.json"

    def test_missing_file_means_single_backend_mode(self):
        self.assertEqual(load_specs(self.path), {})
        self.assertEqual(load_specs(""), {})

    def test_reads_ids_kinds_and_options(self):
        self.path.write_text(
            json.dumps(
                {
                    "failover": False,
                    "health_seconds": 15,
                    "backends": [
                        {
                            "id": "home",
                            "kind": "irodori",
                            "display": "自宅GPU",
                            "url": "http://192.168.0.22:8088",
                            "voice_order": ["a", "b"],
                        },
                        {"id": "local", "kind": "voicevox", "url": "http://127.0.0.1:50021"},
                        {"id": "home", "kind": "irodori"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = load_specs(self.path)

        self.assertFalse(config["failover"])
        self.assertEqual(config["health_seconds"], 15)
        self.assertEqual([spec.id for spec in config["specs"]], ["home", "local"])
        self.assertEqual(config["specs"][0].label(), "自宅GPU")

    def test_broken_json_is_reported(self):
        self.path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(TTSBackendError):
            load_specs(self.path)

        self.path.write_text(json.dumps({"backends": "nope"}), encoding="utf-8")

        with self.assertRaises(TTSBackendError):
            load_specs(self.path)

    def test_options_become_environment_overrides(self):
        spec = BackendSpec(
            id="home",
            kind="irodori",
            options={
                "url": "http://10.0.0.5:8088",
                "voice": "こしあん",
                "voice_order": ["こしあん", "あんこ"],
                "env": {"IRODORI_CAPTION": "ささやき"},
            },
        )

        env = spec.env_for({"IRODORI_URL": "http://old:8088", "KEEP": "1"})

        self.assertEqual(env["TTS_IRODORI_URL"], "http://10.0.0.5:8088")
        self.assertEqual(env["TTS_IRODORI_VOICE"], "こしあん")
        self.assertEqual(env["TTS_IRODORI_VOICE_ORDER"], "こしあん,あんこ")
        self.assertEqual(env["IRODORI_CAPTION"], "ささやき")
        self.assertEqual(env["KEEP"], "1")


class ProviderInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_can_be_built_from_provider_mapping(self):
        providers = {"one": FakeBackend("one"), "two": FakeBackend("two")}
        router = TTSRouter(
            specs(("one", "One"), ("two", "Two")),
            providers=providers,
        )
        self.assertIs(router.backends["one"], providers["one"])
        self.assertIs(router.backends["two"], providers["two"])


if __name__ == "__main__":
    unittest.main()
