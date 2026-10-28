import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.reader.core import ReaderCog


def interaction(user_id):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


class TTSSwitchUsersTests(unittest.TestCase):
    def check(self, user_id, owner_id=0, switch_ids=frozenset()):
        cog = ReaderCog.__new__(ReaderCog)
        cog.config.owner_id = owner_id
        cog.config.tts_switch_user_ids = switch_ids
        target = interaction(user_id)
        allowed = asyncio.run(ReaderCog._tts_owner_only(cog, target))
        return allowed, target.response.send_message.await_count

    def test_owner_and_listed_users_pass(self):
        switch_ids = frozenset({7})
        self.assertEqual(self.check(42, 42, switch_ids), (True, 0))
        self.assertEqual(self.check(7, 42, switch_ids), (True, 0))
        self.assertEqual(self.check(8, 42, switch_ids), (False, 1))

    def test_listed_user_passes_without_owner(self):
        self.assertEqual(self.check(7, 0, frozenset({7})), (True, 0))
        self.assertEqual(self.check(0, 0, frozenset({7})), (False, 1))


if __name__ == "__main__":
    unittest.main()
