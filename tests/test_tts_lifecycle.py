"""切替時のTTSライフサイクル（他のTTSを止めて、選んだ先を起動する）。"""

import unittest

from jllm_plugins.tts_lifecycle.service import (
    LOCAL_ONLY,
    TARGETS,
    UNMANAGED_HOSTS,
    LifecycleError,
    TTSLifecycle,
)
from jllm_tts import TTSBackend, TTSBackendError


class FakeHost:
    """``tts-lifecycle.py`` の応答を真似る。"""

    def __init__(self, *, stopped=(), healthy=True, started=True, error=None, seconds=1.0, log=""):
        self.name = "fake"
        self.calls = []
        self.stopped = list(stopped)
        self.healthy = healthy
        self.started = started
        self.error = error
        self.seconds = seconds
        self.log = log

    async def run(self, command):
        self.calls.append(command)

        if self.error:
            raise LifecycleError(self.error)

        if command == "stop all":
            return {"ok": True, "action": "stop", "stopped": list(self.stopped)}

        return {
            "ok": True,
            "action": "switch",
            "stopped": list(self.stopped),
            "started": self.started,
            "healthy": self.healthy,
            "seconds": self.seconds,
            "error": "",
            "log": self.log,
        }


class FakeLifecycle:
    configured = True

    def __init__(self, result=None):
        self.ids = []
        self.result = result or {
            "configured": True,
            "stopped": ["gpu:ming"],
            "started": True,
            "healthy": True,
            "seconds": 12.0,
            "error": "",
            "warnings": [],
        }

    async def ensure(self, ident):
        self.ids.append(ident)
        return dict(self.result)


class FakeBackend(TTSBackend):
    def __init__(self, ident, *, failures=0):
        self.id = ident
        self.failures = failures
        self.health_calls = 0

    def describe(self):
        return f"fake({self.id})"

    async def list_voices(self):
        return []

    async def synthesize(self, text, speaker_id, speed=1.0):
        return b"wav"

    async def health(self):
        self.health_calls += 1

        if self.health_calls <= self.failures:
            raise TTSBackendError(f"{self.id} に接続できません")


class MappingTests(unittest.TestCase):
    def test_each_backend_maps_to_a_host_and_a_unit(self):
        self.assertEqual(TARGETS["home"], ("gpu", "irodori"))
        self.assertEqual(TARGETS["speechcpp"], ("gpu", "speechcpp"))
        self.assertEqual(TARGETS["ming"], ("gpu", "ming"))
        self.assertEqual(TARGETS["home-drama3"], ("gpu", "drama3"))
        self.assertEqual(TARGETS["z170"], ("z170", "speechcpp"))
        self.assertEqual(TARGETS["z170-ts"], ("z170", "speechcpp"))
        self.assertEqual(TARGETS["z170-irodori"], ("z170", "irodori"))
        self.assertIsNone(TARGETS["x230-voicevox"])
        self.assertEqual(LOCAL_ONLY, {"x230-voicevox"})

    def test_no_mapping_for_untouchable_services(self):
        self.assertNotIn("d1-local", TARGETS)
        self.assertNotIn("anima-mobile", TARGETS)
        self.assertNotIn("jllm-translation", TARGETS)

    def test_nuko_is_an_unmanaged_external_host(self):
        """外部ホスト（管理スクリプト無し）。起動先は持つが、操作はしない。"""

        self.assertEqual(TARGETS["nuko"], ("nuko", "drama3"))
        self.assertEqual(UNMANAGED_HOSTS, {"nuko"})


class EnsureTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_host_is_switched_and_other_hosts_are_stopped(self):
        gpu, z170 = FakeHost(stopped=["ming"]), FakeHost()
        lifecycle = TTSLifecycle({"gpu": gpu, "z170": z170})

        result = await lifecycle.ensure("home")

        self.assertEqual(gpu.calls, ["switch irodori"])
        self.assertEqual(z170.calls, ["stop all"])
        self.assertEqual(set(result["stopped"]), {"gpu:ming"})
        self.assertTrue(result["healthy"])
        self.assertEqual(result["error"], "")

    async def test_the_target_host_also_stops_its_other_units(self):
        gpu = FakeHost(stopped=["ming", "speechcpp"])
        lifecycle = TTSLifecycle({"gpu": gpu})

        result = await lifecycle.ensure("home")

        self.assertEqual(gpu.calls, ["switch irodori"])
        self.assertIn("gpu:ming", result["stopped"])

    async def test_local_engine_only_stops_the_remote_ones(self):
        gpu, z170 = FakeHost(stopped=["irodori"]), FakeHost(stopped=["speechcpp"])
        lifecycle = TTSLifecycle({"gpu": gpu, "z170": z170})

        result = await lifecycle.ensure("x230-voicevox")

        self.assertEqual(gpu.calls, ["stop all"])
        self.assertEqual(z170.calls, ["stop all"])
        self.assertIsNone(result["target"])
        self.assertEqual(result["stopped"], ["gpu:irodori", "z170:speechcpp"])
