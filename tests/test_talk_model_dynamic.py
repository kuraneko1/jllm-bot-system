import unittest
from types import SimpleNamespace

from jllm_plugins.translation.cog import TranslationCog, TranslationCogConfig


class TalkModelTests(unittest.TestCase):
    def test_new_sessions_use_current_client_model(self):
        cog = TranslationCog.__new__(TranslationCog)
        cog.config = TranslationCogConfig(talk_model="old")
        cog.client = SimpleNamespace(model="new")
        self.assertEqual(cog.talk_model(), "new")
        cog.client.model = "newer"
        self.assertEqual(cog.talk_model(), "newer")

    def test_client_without_model_uses_config(self):
        cog = TranslationCog.__new__(TranslationCog)
        cog.config = TranslationCogConfig(talk_model="configured")
        cog.client = SimpleNamespace()
        self.assertEqual(cog.talk_model(), "configured")
