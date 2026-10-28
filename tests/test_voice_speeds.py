# ruff: noqa: E501
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes
from jllm_tts import Voice


class VoiceSpeedsTests(unittest.TestCase):
    def test_unset_is_none_and_invalid_values_rejected(self):
        store = VoiceSpeeds()
        self.assertIsNone(store.get("nobody"))
        for bad in (0.4, 2.1, 0, float("nan"), float("inf"), "fast", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.set("voice", bad)
        self.assertIsNone(store.get("voice"))

    def test_boundaries_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/voice_speeds.json"
            store = VoiceSpeeds(path)
            self.assertEqual(store.set("a", 0.5), 0.5)
            self.assertEqual(store.set("b", 2.0), 2.0)
            self.assertEqual(store.set("c", 1.25), 1.25)
            reloaded = VoiceSpeeds(path)
            self.assertEqual(reloaded.get("a"), 0.5)
            self.assertEqual(reloaded.get("b"), 2.0)
            self.assertEqual(reloaded.get("c"), 1.25)
            self.assertTrue(reloaded.remove("c"))
            self.assertFalse(reloaded.remove("c"))
            self.assertIsNone(VoiceSpeeds(path).get("c"))

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/voice_speeds.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            self.assertIsNone(VoiceSpeeds(path).get("a"))


def make_reader(tts):
    cog = ReaderCog.__new__(ReaderCog)
    cog.tts = tts
    cog.voice_volumes = VoiceVolumes()
    cog.voice_speeds = VoiceSpeeds()
    cog._voice_id_names = {}
    return cog


class FakeTTS:
    def __init__(self, voices=None, fail=False):
        self.voices = voices or [Voice(id=1, name="none"), Voice(id=2, name="Clone Ryu")]
        self.fail = fail

    async def list_voices(self):
        if self.fail:
            raise RuntimeError("tts down")
        return self.voices


class TtsSpeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_and_id_lookup(self):
        cog = make_reader(FakeTTS())
        self.assertIsNone(await cog.tts_speed_for("Clone Ryu"))
        cog.voice_speeds.set("Clone Ryu", 1.5)
        self.assertEqual(await cog.tts_speed_for("Clone Ryu"), 1.5)
        self.assertEqual(await cog.tts_speed_for(2), 1.5)
        self.assertIsNone(await cog.tts_speed_for("none"))

    async def test_unknown_id_and_backend_failure_fall_back(self):
        cog = make_reader(FakeTTS())
        self.assertIsNone(await cog.tts_speed_for(99))
        failing = make_reader(FakeTTS(fail=True))
        self.assertIsNone(await failing.tts_speed_for(2))


class ReaderSpeedAppliesTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, speed=None):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=2, enqueue=Mock(),
        )
        cog.players = {1: player}
        cog.dictionary = SimpleNamespace(
            split=lambda value: [SimpleNamespace(kind='text', value=value)]
        )
        cog.readings = SimpleNamespace(apply=lambda value: value)
        cog.user_voices = {}
        cog.forced_voices = {}
        cog.voice_volumes = VoiceVolumes()
        cog.voice_speeds = VoiceSpeeds()
        if speed is not None:
            cog.voice_speeds.set("Clone Ryu", speed)
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(
            list_voices=AsyncMock(return_value=[Voice(id=2, name="Clone Ryu")])
        )
        return cog, player

    async def send(self, cog, text):
        message = SimpleNamespace(
            id=31,
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content=text, clean_content=text,
        )
        with patch.object(cog.config, 'allowed_channel_ids', {123}):
            await cog.on_message(message)

    async def test_voice_speed_is_default_without_inline_tag(self):
        cog, player = self.make_cog(speed=1.5)
        await self.send(cog, "こんにちは")
        player.enqueue.assert_called_once_with("こんにちは", None, 1.5, '31', '1:2', volume=1.0, effects=())

    async def test_unset_voice_speed_keeps_default(self):
        cog, player = self.make_cog()
        await self.send(cog, "こんにちは")
        player.enqueue.assert_called_once_with("こんにちは", None, 1.2, '31', '1:2', volume=1.0, effects=())

    async def test_inline_tag_overrides_voice_speed(self):
        cog, player = self.make_cog(speed=1.5)
        await self.send(cog, "A f2 B")
        player.enqueue.assert_any_call("A", None, 2.0, '31', '1:2', volume=1.0, effects=())
        player.enqueue.assert_any_call("B", None, 1.5, '31', '1:2', volume=1.0, effects=())

    async def test_effect_token_reaches_enqueue(self):
        cog, player = self.make_cog()
        await self.send(cog, "こんにちは rb ch3")
        player.enqueue.assert_called_once_with(
            "こんにちは", None, 1.2, '31', '1:2', volume=1.0,
            effects=(("rb", 1.0), ("ch", 3.0)),
        )

    async def test_effect_token_on_wav_segment(self):
        cog, player = self.make_cog()
        cog.toggles = {"wav": True}
        cog.dictionary = SimpleNamespace(
            split=lambda value: [SimpleNamespace(kind='sound', value='/s/x.wav')],
            volume_of_file=lambda name: 1.0,
        )
        player.enqueue_sound = Mock()
        message = SimpleNamespace(
            id=32,
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content="x rb2", clean_content="x rb2",
        )
        with patch.object(cog.config, 'allowed_channel_ids', {123}):
            await cog.on_message(message)
        player.enqueue_sound.assert_called_once()
        self.assertEqual(player.enqueue_sound.call_args.kwargs["effects"], (("rb", 2.0),))


if __name__ == "__main__":
    unittest.main()
