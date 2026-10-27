import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jllm_plugins.miq.command import parse
from jllm_plugins.miq.trigger import is_miq_image, is_miq_trigger
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class VoiceSettingStoreTests(unittest.TestCase):
    def test_speed_and_volume_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            speeds = VoiceSpeeds(Path(temp, "speeds.json"))
            volumes = VoiceVolumes(Path(temp, "volumes.json"))
            self.assertEqual(speeds.set("voice", 1.5), 1.5)
            self.assertEqual(volumes.set("voice", 0.4), 0.4)
            self.assertEqual(VoiceSpeeds(speeds.path).get("voice"), 1.5)
            self.assertEqual(VoiceVolumes(volumes.path).get("voice"), 0.4)
            self.assertTrue(speeds.remove("voice"))
            self.assertTrue(volumes.remove("voice"))
            with self.assertRaises(ValueError):
                speeds.set("bad", 0.1)
            with self.assertRaises(ValueError):
                volumes.set("bad", 4.0)


class MiqTriggerTests(unittest.TestCase):
    def test_command_and_natural_language_are_distinct(self):
        self.assertTrue(is_miq_trigger("miq"))
        self.assertTrue(is_miq_trigger("Ｍｉｑ　縦"))
        self.assertFalse(is_miq_trigger("miq ってなに？"))
        command = parse("miqmiq3 縦 cool")
        self.assertEqual(command.count, 2)
        self.assertEqual(command.layout, 3)
        self.assertEqual(command.orientation, "portrait")
        self.assertEqual(command.category, "cool")

    def test_generated_image_is_only_recognized_from_bot(self):
        attachment = SimpleNamespace(filename="miq-example.png")
        message = SimpleNamespace(
            author=SimpleNamespace(bot=True),
            attachments=[attachment],
        )
        self.assertTrue(is_miq_image(message))
        message.author.bot = False
        self.assertFalse(is_miq_image(message))
        message.author.bot = True
        message.attachments = []
        self.assertFalse(is_miq_image(message))


if __name__ == "__main__":
    unittest.main()
