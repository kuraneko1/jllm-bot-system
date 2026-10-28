# ruff: noqa: E501
"""TTS バックエンドの規約とレジストリのテスト。

``CustomBackend`` は docs/backends.md の例と同じ最小構成。
新しいバックエンドが増えたら、このファイルのテストを踏ませることを目安にする。
"""

import unittest

from jllm_plugins.tts_registry import (
    TTSBackend,
    TTSBackendError,
    Voice,
    VoicevoxError,
    available,
    build_backend,
    register,
)


class CustomBackend(TTSBackend):
    """規約を満たすだけの最小実装（未登録）。"""

    def __init__(self, url: str = "", api_key: str = ""):
        self.url = url
        self.api_key = api_key
        self.requests: list[tuple[str, str | int, float]] = []

    @classmethod
    def from_env(cls, env: dict[str, str]) -> CustomBackend:
        return cls(
            url=env.get("TTS_CUSTOM_URL", ""),
            api_key=env.get("TTS_CUSTOM_API_KEY", ""),
        )

    async def list_voices(self) -> list[Voice]:
        return [Voice(id="a", name="A"), Voice(id=2, name="B")]

    async def synthesize(
        self,
        text: str,
        speaker_id: str | int,
        speed: float = 1.0,
    ) -> bytes:
        self.requests.append((text, speaker_id, speed))

        return b"RIFFfake"


@register("custom-for-test")
class RegisteredCustomBackend(CustomBackend):
    """``@register`` 経由で選べるようにした版。"""


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_backends_registered(self):
        self.assertIn("voicevox", available())
        self.assertIn("irodori", available())

    async def test_unknown_backend_lists_choices(self):
        with self.assertRaises(TTSBackendError) as ctx:
            build_backend("no-such-backend", env={})

        self.assertIn("voicevox", str(ctx.exception))

    async def test_legacy_env_is_still_honored(self):
        backend = build_backend(
            "voicevox",
            env={"VOICEVOX_URL": "http://example.jp:50021/"},
        )

        self.assertEqual(backend.base_url, "http://example.jp:50021")
        await backend.close()

    async def test_namespaced_env_wins_over_legacy(self):
        backend = build_backend(
            "voicevox",
            env={
                "VOICEVOX_URL": "http://legacy.example",
                "TTS_VOICEVOX_URL": "http://new.example",
            },
        )

        self.assertEqual(backend.base_url, "http://new.example")
        await backend.close()

    async def test_irodori_defaults(self):
        backend = build_backend("irodori", env={})

        self.assertEqual(backend.base_url, "http://127.0.0.1:8088")
        self.assertEqual(backend.default_voice, "none")
        self.assertEqual(backend.display_name, "Irodori")

        await backend.close()
        self.assertTrue(backend.session.closed)

    async def test_error_alias(self):
        self.assertIs(VoicevoxError, TTSBackendError)

    async def test_voicevox_maps_foreign_speaker_names_to_a_style(self):
        # 退避先になったとき、他エンジンのクローン名がそのまま来る（422 で全滅していた）
        backend = build_backend("voicevox", env={"TTS_VOICEVOX_SPEAKER": "8"})
        backend._voices = [Voice(id=3, name="ノーマル"), Voice(id=8, name="あまあま")]

        self.assertEqual(await backend._resolve_speaker(3), 3)
        self.assertEqual(await backend._resolve_speaker("3"), 3)
        self.assertEqual(await backend._resolve_speaker("ノーマル"), 3)
        self.assertEqual(await backend._resolve_speaker("クローンぺんたv3"), 8)
        self.assertEqual(await backend._resolve_speaker(999), 8)

        backend.default_speaker = None
        self.assertEqual(await backend._resolve_speaker("クローンぺんたv3"), 3)
        await backend.close()


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_backend_is_buildable(self):
        backend = build_backend(
            "custom-for-test",
            env={"TTS_CUSTOM_URL": "http://example"},
        )

        self.assertIsInstance(backend, CustomBackend)
        self.assertEqual(backend.url, "http://example")

    async def test_speakers_view_shape(self):
        backend = build_backend("custom-for-test", env={})
        speakers = await backend.speakers()

        self.assertEqual(
            speakers,
            [
                {
                    "name": "custom-for-test",
                    "styles": [
                        {"id": "a", "name": "A"},
                        {"id": 2, "name": "B"},
                    ],
                }
            ],
        )

    async def test_synthesize_passes_voice_id_through(self):
        backend = build_backend("custom-for-test", env={})
        audio = await backend.synthesize(" hello ", "a", 1.5)

        self.assertEqual(audio, b"RIFFfake")
        self.assertEqual(backend.requests, [(" hello ", "a", 1.5)])
