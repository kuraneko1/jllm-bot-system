import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from jllm_plugins.miq.plugin import Plugin
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.storage.plugin import LocalStorage

from jllm.kernel import ProfileError, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "independent.yaml"


class MiqConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_uses_normalized_configured_words(self):
        with tempfile.TemporaryDirectory() as temp:
            gateway = SimpleNamespace(
                bot=SimpleNamespace(), add_cog=AsyncMock(return_value=lambda: None)
            )
            context = SimpleNamespace(
                config={"words": [" Quote ", "Q"]},
                dependencies={"storage.local": LocalStorage(temp), "discord.gateway": gateway,
                              "policy.restrictions": UserRestrictions()},
                scope=SimpleNamespace(add=Mock()),
            )
            service = (await Plugin().setup(context))["feature.miq"]
            self.assertEqual(service.config.words, ("quote", "q"))
            self.assertTrue(service.matches_trigger("ＱＵＯＴＥ"))
            self.assertTrue(service.matches_trigger("qq"))
            self.assertFalse(service.matches_trigger("miq"))
            gateway.add_cog.assert_awaited_once_with(service)

    def test_empty_and_whitespace_words_are_rejected_by_schema(self):
        for words in ([], [""], ["   "], ["miq", ""]):
            with self.subTest(words=words):
                profile = load_profile(PROFILE)
                profile.plugins["feature.miq"]["config"] = {"words": words}
                with self.assertRaises(ProfileError):
                    resolve(profile, discover())


if __name__ == "__main__":
    unittest.main()
