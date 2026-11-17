import unittest
from types import SimpleNamespace

from jllm_plugins.reader.core import ReaderConfig, is_bot_admin
from jllm_plugins.translation.cog import TranslationCogConfig, can_manage


def member(user_id, role_ids=(), manage=False):
    return SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(manage_guild=manage),
    )


class BotAdminTests(unittest.TestCase):
    def test_owner_is_admin(self):
        self.assertTrue(is_bot_admin(member(1), 1, frozenset({9})))

    def test_admin_role_is_admin(self):
        self.assertTrue(is_bot_admin(member(2, [9]), 1, frozenset({9})))

    def test_other_role_is_not_admin(self):
        self.assertFalse(is_bot_admin(member(2, [8]), 1, frozenset({9})))

    def test_no_roles_configured_keeps_owner_only(self):
        self.assertFalse(is_bot_admin(member(2, [9]), 1, frozenset()))

    def test_plain_user_is_not_admin(self):
        self.assertFalse(is_bot_admin(member(3), 1, frozenset({9})))


class CanManageTests(unittest.TestCase):
    def test_owner_can_manage(self):
        self.assertTrue(can_manage(member(1), 1))

    def test_manage_guild_can_manage(self):
        self.assertTrue(can_manage(member(2, manage=True), 1))

    def test_admin_role_can_manage(self):
        self.assertTrue(can_manage(member(2, [9]), 1, frozenset({9})))

    def test_other_role_cannot_manage(self):
        self.assertFalse(can_manage(member(2, [8]), 1, frozenset({9})))

    def test_plain_user_cannot_manage(self):
        self.assertFalse(can_manage(member(3), 1))


class ConfigDefaultsTests(unittest.TestCase):
    def test_admin_roles_default_to_empty(self):
        self.assertEqual(ReaderConfig().admin_role_ids, frozenset())
        self.assertEqual(TranslationCogConfig().admin_role_ids, frozenset())


if __name__ == "__main__":
    unittest.main()
