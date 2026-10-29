"""切替時のTTSライフサイクル（他のTTSを止めて、選んだ先を起動する）。"""

import unittest

from jllm_plugins.tts_lifecycle.service import (
    LifecycleError,
    TTSLifecycle,
    build_tts_lifecycle,
)
from jllm_tts import TTSBackend, TTSBackendError

#: テスト用の割り当て。実運用では profile の targets で指定する。
TARGETS = {
    "home": ("gpu", "irodori"),
    "speechcpp": ("gpu", "speechcpp"),
    "ming": ("gpu", "ming"),
    "nuko": ("nuko", "drama3"),
}
LOCAL_ONLY = {"x230-voicevox"}
UNMANAGED = {"nuko"}


def lifecycle_with(hosts):
    return TTSLifecycle(
        dict(hosts),
        targets=TARGETS,
        local_only=LOCAL_ONLY,
        unmanaged_hosts=UNMANAGED,
    )


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
    env = {
        "TTS_CONTROL_HOST": "gpu.example",
        "TTS_CONTROL_KEY": "/k",
        "TTS_CONTROL_KNOWN_HOSTS": "/kh",
    }

    def test_targets_come_from_the_configuration(self):
        service = build_tts_lifecycle(
            env=self.env,
            config={
                "targets": {"home": ["gpu", "irodori"], "local": None},
                "local_only": ["local"],
                "unmanaged_hosts": ["external"],
            },
        )

        self.assertEqual(service.targets["home"], ("gpu", "irodori"))
        self.assertIsNone(service.targets["local"])
        self.assertEqual(service.local_only, {"local"})
        self.assertEqual(service.unmanaged_hosts, {"external"})

    def test_without_configuration_nothing_is_started(self):
        service = build_tts_lifecycle(env=self.env, config={})

        self.assertEqual(service.targets, {})
        self.assertEqual(service.hosts, {"gpu": service.hosts["gpu"]})

    def test_missing_control_environment_disables_lifecycle(self):
        service = build_tts_lifecycle(env={}, config={"targets": {"home": ["gpu", "irodori"]}})

        self.assertFalse(service.configured)


class EnsureTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_host_is_switched_and_other_hosts_are_stopped(self):
        gpu, z170 = FakeHost(stopped=["ming"]), FakeHost()
        lifecycle = lifecycle_with({"gpu": gpu, "z170": z170})

        result = await lifecycle.ensure("home")

        self.assertEqual(gpu.calls, ["switch irodori"])
        self.assertEqual(z170.calls, ["stop all"])
        self.assertEqual(set(result["stopped"]), {"gpu:ming"})
        self.assertTrue(result["healthy"])
        self.assertEqual(result["error"], "")

    async def test_the_target_host_also_stops_its_other_units(self):
        gpu = FakeHost(stopped=["ming", "speechcpp"])
        lifecycle = lifecycle_with({"gpu": gpu})

        result = await lifecycle.ensure("home")

        self.assertEqual(gpu.calls, ["switch irodori"])
        self.assertIn("gpu:ming", result["stopped"])

    async def test_local_engine_only_stops_the_remote_ones(self):
        gpu, z170 = FakeHost(stopped=["irodori"]), FakeHost(stopped=["speechcpp"])
        lifecycle = lifecycle_with({"gpu": gpu, "z170": z170})

        result = await lifecycle.ensure("x230-voicevox")

        self.assertEqual(gpu.calls, ["stop all"])
        self.assertEqual(z170.calls, ["stop all"])
        self.assertIsNone(result["target"])
        self.assertEqual(result["stopped"], ["gpu:irodori", "z170:speechcpp"])
