# ruff: noqa: E501
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.talk_log import TalkLog
from jllm_plugins.llm.translation import (
    TranslationClient,
    TranslationSettings,
    translation_pages,
    translation_text,
)
from jllm_plugins.llm.translation_control import TranslationControlError
from jllm_plugins.translation.cog import TranslationCog, TranslationView


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = TranslationSettings(Path(self.directory.name) / "settings.json")
        self.client = SimpleNamespace(translate=AsyncMock(return_value="こんにちは"), close=AsyncMock())
        self.controller = SimpleNamespace(set_enabled=AsyncMock())
        self.cog = TranslationCog(None, self.settings, self.client, self.controller, TalkLog(None, None),
                       SessionStore(Path(self.directory.name) / 'sessions'))
        self.cog.backend_ready = True
        self.whitelist = patch.object(self.cog.config, "allowed_channel_ids", {10})
        self.whitelist.start()
        self.addCleanup(self.whitelist.stop)
        self.owner = patch.object(self.cog.config, "owner_id", 1)
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.member = SimpleNamespace(id=2, bot=False, display_name="Somebody", voice=SimpleNamespace(channel=SimpleNamespace(id=3, name="General")))
        self.guild = SimpleNamespace(id=4, get_member=lambda uid: self.member if uid == 2 else None,
                                     voice_channels=[SimpleNamespace(members=[self.member])], stage_channels=[])
        self.settings.change(4, 10, True, [2])
        self.member.guild = self.guild

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def message(self):
        return SimpleNamespace(id=100, guild=self.guild, channel=SimpleNamespace(id=10), author=self.member,
                               content="Hello", clean_content="Hello", webhook_id=None, reply=AsyncMock())

    def interaction(self, uid=1):
        return SimpleNamespace(user=SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=False)),
                               guild=self.guild, guild_id=4, channel_id=10,
                               response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    async def test_public_reply_and_persistence(self):
        restored = TranslationSettings(self.settings.path)
        self.assertEqual(restored.users(4, 10), {2})
        await self.cog.cog_load()
        message = self.message()
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.client.translate.assert_awaited_once_with("Hello")
        message.reply.assert_awaited_once()
        args = message.reply.call_args
        self.assertIn("**原文**", args.args[0])
        self.assertIn("**日本語訳**", args.args[0])
        self.assertNotIn("ephemeral", args.kwargs)
        self.assertFalse(args.kwargs["allowed_mentions"].everyone)

    async def test_off_during_inference_and_reenable_discards_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(text):
            started.set()
            await release.wait()
            return "こんにちは"
        self.client.translate.side_effect = delayed
        await self.cog.cog_load()
        message = self.message()
        await self.cog.on_message(message)
        await asyncio.wait_for(started.wait(), 1)
        self.settings.change(4, 10, False)
        self.settings.change(4, 10, True, [2])
        release.set()
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_not_awaited()

    async def test_off_discards_queued_and_vc_exit_discards_result(self):
        message = self.message()
        await self.cog.on_message(message)
        self.settings.change(4, 10, False)
        await self.cog.cog_load()
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.client.translate.assert_not_awaited()
        await self.cog.set_enabled(4, 10, True, [2])
        async def leave(text):
            self.member.voice = None
            return "こんにちは"
        self.client.translate.side_effect = leave
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_not_awaited()

    async def test_unrelated_channel_bot_webhook_empty_and_non_vc_ignored(self):
        for field, value in [("channel", SimpleNamespace(id=999)), ("webhook_id", 99), ("content", " ")]:
            message = self.message()
            setattr(message, field, value)
            await self.cog.on_message(message)
        self.member.bot = True
        await self.cog.on_message(self.message())
        self.member.bot = False
        self.member.voice = None
        await self.cog.on_message(self.message())
        self.assertTrue(self.cog.queue.empty())

    async def test_non_text_posts_never_queued(self):
        for text in ("https://example.com/a.gif", "<https://example.com>", "www.example.com", "😀👍", "1️⃣", "<:happy:123>", "<a:happy:123>", "<@123> <#456>", "@everyone", "!? ... 123", "```python\nprint('hello')\n```", "`hello`", ""):
            with self.subTest(text=text):
                message = self.message()
                message.content = text
                message.stickers = [SimpleNamespace(name="sticker")]
                message.attachments = [SimpleNamespace(filename="picture.png")]
                await self.cog.on_message(message)
        self.assertTrue(self.cog.queue.empty())
        self.client.translate.assert_not_awaited()

    async def test_mixed_post_translates_text_only_and_keeps_original(self):
        message = self.message()
        message.content = "Hello <@123> 😀 https://example.com/a.gif"
        message.clean_content = "Hello @somebody 😀 https://example.com/a.gif"
        await self.cog.cog_load()
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.client.translate.assert_awaited_once_with("Hello")
        self.assertIn(message.clean_content, message.reply.call_args.args[0])

    async def test_filter_preserves_language_and_markdown_link_labels(self):
        self.assertEqual(translation_text("Please [check here](https://example.com/a) soon."), "Please check here soon.")
        self.assertEqual(translation_text("Bonjour!\nこんにちは 👋"), "Bonjour!\nこんにちは")
        self.assertEqual(translation_text("See `print(1)` please :smile:"), "See please")

    async def test_permissions_commands_and_panel_owner(self):
        interaction = self.interaction(9)
        await TranslationCog.translate.callback(self.cog, interaction)
        self.assertEqual(self.settings.users(4, 10), {2})
        interaction.followup.send.assert_not_awaited()
        owner = self.interaction()
        await TranslationCog.translate.callback(self.cog, owner)
        args = owner.followup.send.call_args.kwargs
        self.assertIsInstance(args["view"], TranslationView)
        self.assertEqual(args["embed"].title, "🌐 このチャンネルの翻訳・会話設定")
        self.assertFalse(await args["view"].interaction_check(interaction))
        self.controller.set_enabled.assert_not_awaited()

    async def test_last_disable_stops_model_but_other_channel_keeps_it(self):
        self.settings.change(6, 11, True, [5])
        await self.cog.set_enabled(4, 10, False)
        self.controller.set_enabled.assert_not_awaited()
        await self.cog.set_enabled(6, 11, False)
        self.controller.set_enabled.assert_awaited_once_with(False)
        self.assertFalse(self.cog.backend_ready)

    async def test_start_failure_keeps_off_and_displays_memory_error(self):
        self.settings.change(4, 10, False)
        self.controller.set_enabled.side_effect = [TranslationControlError("空きVRAMが不足しています"), None]
        with self.assertLogs("jllm_plugins.translation.cog", level="ERROR"):
            interaction = self.interaction()
            view = TranslationView(self.cog, interaction)
            view.users = {2}
            view.pending_enabled = True
            await view.apply_button.callback(interaction)
        self.assertFalse(self.settings.any_enabled())
        self.assertFalse(self.cog.backend_ready)
        self.assertIn("空きVRAM", str(interaction.edit_original_response.call_args.kwargs["embed"].to_dict()))
        self.assertFalse(view.finished)
        self.assertEqual(self.controller.set_enabled.call_args.args, (False,))

    async def test_start_before_save_and_stop_failure_does_not_claim_release(self):
        self.settings.change(4, 10, False)
        async def start(enabled):
            self.assertFalse(self.settings.any_enabled())
        self.controller.set_enabled.side_effect = start
        await self.cog.set_enabled(4, 10, True, [2])
        self.assertTrue(self.settings.any_enabled())
        self.controller.set_enabled.side_effect = TranslationControlError("停止できません")
        with self.assertRaises(TranslationControlError):
            await self.cog.set_enabled(4, 10, False)
        self.assertFalse(self.settings.any_enabled())
        self.assertNotIn("解放済み", self.cog.status(self.interaction()))

    async def test_original_toggle_persists_and_hides_in_public_output(self):
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        view.original_select._values = ["hide"]
        await view.original_select.callback(interaction)
        self.assertTrue(self.settings.show_original(4, 10))
        await view.apply_button.callback(interaction)
        self.assertTrue(view.finished)
        self.assertIsNone(interaction.edit_original_response.call_args.kwargs["view"])
        self.assertFalse(TranslationSettings(self.settings.path).show_original(4, 10))
        await self.cog.cog_load()
        message = self.message()
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertNotIn("**原文**", message.reply.call_args.args[0])
        self.assertNotIn("Hello", message.reply.call_args.args[0])

    async def test_participant_pages_selection_defaults_and_buttons(self):
        members = [SimpleNamespace(id=100+i, bot=False, display_name=f"User {i:02}", voice=self.member.voice) for i in range(30)]
        self.guild.voice_channels = [SimpleNamespace(members=members)]
        view = TranslationView(self.cog, self.interaction())
        self.assertEqual(len(view.selector.options), 25)
        view.selector._values = ["100", "101"]
        await view.selector.callback(self.interaction())
        self.assertEqual(view.users, {100, 101})
        self.assertTrue(next(o for o in view.selector.options if o.value == "100").default)
        self.assertFalse(view.apply_button.disabled)
        await view.next_page.callback(self.interaction())
        self.assertEqual(view.page, 1)
        self.assertEqual(len(view.selector.options), 6)  # includes configured member
        view.selector._values = ["129"]
        await view.selector.callback(self.interaction())
        self.assertEqual(view.users, {100, 101, 129})
        await view.previous.callback(self.interaction())
        self.assertTrue(next(o for o in view.selector.options if o.value == "100").default)

    async def test_empty_participant_list_disables_selector(self):
        self.settings.change(4, 10, False)
        self.guild.voice_channels = []
        view = TranslationView(self.cog, self.interaction())
        self.assertTrue(view.selector.disabled)
        self.assertFalse(view.apply_button.disabled)
        self.assertTrue(view.enabled_select.options[1].default)

    async def test_panel_changes_are_pending_until_apply_and_replace_targets(self):
        other = SimpleNamespace(id=3, bot=False, display_name="Other", voice=self.member.voice)
        self.guild.voice_channels[0].members.append(other)
        self.guild.get_member = lambda uid: {2: self.member, 3: other}.get(uid)
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        view.selector._values = ["3"]
        await view.selector.callback(interaction)
        self.assertEqual(self.settings.users(4, 10), {2})
        self.controller.set_enabled.assert_not_awaited()
        await view.apply_button.callback(interaction)
        self.assertEqual(self.settings.users(4, 10), {3})
        self.assertTrue(view.finished)
        self.assertIsNone(interaction.edit_original_response.call_args.kwargs["view"])

    async def test_panel_off_stops_model_only_on_confirm(self):
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        view.enabled_select._values = ["off"]
        await view.enabled_select.callback(interaction)
        self.assertTrue(self.settings.any_enabled())
        self.controller.set_enabled.assert_not_awaited()
        await view.apply_button.callback(interaction)
        self.assertFalse(self.settings.any_enabled())
        self.controller.set_enabled.assert_awaited_once_with(False)

    async def test_cancel_and_timeout_discard_pending_choices(self):
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        view.pending_enabled = False
        await view.cancel.callback(interaction)
        self.assertEqual(self.settings.users(4, 10), {2})
        self.assertTrue(view.finished)
        view = TranslationView(self.cog, interaction)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertEqual(self.settings.users(4, 10), {2})
        self.assertIsNone(view.message.edit.call_args.kwargs["view"])

    async def test_panel_current_choices_have_dropdown_defaults(self):
        self.settings.set_show_original(4, 10, False)
        view = TranslationView(self.cog, self.interaction())
        self.assertTrue(view.selector.options[0].default)
        self.assertTrue(view.enabled_select.options[0].default)
        self.assertTrue(view.original_select.options[1].default)

    async def test_startup_reconciles_enabled_and_disabled_model(self):
        self.settings.change(4, 10, False)
        await self.cog.cog_load()
        self.controller.set_enabled.assert_awaited_once_with(False)
        self.assertFalse(self.cog.backend_ready)

    async def test_vc_departure_unregisters_persistently_and_stops_last_model(self):
        self.settings.change(4, 11, True, [2])
        before = SimpleNamespace(channel=self.member.voice.channel)
        self.member.voice = None
        await self.cog.on_voice_state_update(self.member, before, SimpleNamespace(channel=None))
        self.assertFalse(self.settings.any_enabled())
        self.assertFalse(TranslationSettings(self.settings.path).any_enabled())
        self.controller.set_enabled.assert_awaited_once_with(False)
        self.member.voice = SimpleNamespace(channel=before.channel)
        await self.cog.on_voice_state_update(self.member, SimpleNamespace(channel=None), before)
        await self.cog.on_message(self.message())
        self.assertTrue(self.cog.queue.empty())

    async def test_departure_preserves_other_users_and_guild_settings(self):
        self.settings.change(4, 10, True, [3])
        self.settings.change(5, 10, True, [2])
        await self.cog.on_voice_state_update(self.member, SimpleNamespace(channel=self.member.voice.channel), SimpleNamespace(channel=None))
        self.assertEqual(self.settings.users(4, 10), {3})
        self.assertEqual(self.settings.users(5, 10), {2})
        self.controller.set_enabled.assert_not_awaited()

    async def test_voice_moves_mute_changes_and_unrelated_users_are_ignored(self):
        before = SimpleNamespace(channel=self.member.voice.channel)
        for after in (before, SimpleNamespace(channel=SimpleNamespace(id=99))):
            await self.cog.on_voice_state_update(self.member, before, after)
        other = SimpleNamespace(id=99, bot=False, guild=self.guild)
        await self.cog.on_voice_state_update(other, before, SimpleNamespace(channel=None))
        self.assertEqual(self.settings.users(4, 10), {2})
        self.controller.set_enabled.assert_not_awaited()

    async def test_departure_invalidates_result_before_control_lock_is_released(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(text):
            started.set()
            await release.wait()
            return "訳"
        self.client.translate.side_effect = delayed
        await self.cog.cog_load()
        message = self.message()
        await self.cog.on_message(message)
        await started.wait()
        await self.cog.control_lock.acquire()
        task = asyncio.create_task(self.cog.on_voice_state_update(self.member, SimpleNamespace(channel=self.member.voice.channel), SimpleNamespace(channel=None)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_not_awaited()
        self.cog.control_lock.release()
        await task

    async def test_ready_removes_departures_missed_during_downtime(self):
        self.guild.voice_channels = []
        self.guild.unavailable = False
        self.cog.bot = SimpleNamespace(guilds=[self.guild])
        await self.cog.on_ready()
        self.assertFalse(self.settings.any_enabled())
        self.controller.set_enabled.assert_awaited_once_with(False)

    async def test_individual_disable_preserves_others_and_guild_scope(self):
        self.settings.change(4, 10, True, [5])
        self.settings.change(6, 10, True, [2])
        self.settings.change(4, 10, False, [2])
        self.assertEqual(self.settings.users(4, 10), {5})
        self.assertEqual(self.settings.users(6, 10), {2})

    async def test_queue_bounded_and_failure_does_not_kill_worker(self):
        message = self.message()
        for _ in range(34):
            await self.cog.on_message(message)
        self.assertEqual(self.cog.queue.qsize(), 32)
        message.reply.assert_awaited_once()  # rate-limited queue warning
        while not self.cog.queue.empty():
            self.cog.queue.get_nowait()
            self.cog.queue.task_done()
        self.client.translate.side_effect = [TimeoutError(), "成功"]
        await self.cog.cog_load()
        with self.assertLogs("jllm_plugins.translation.cog", level="ERROR"):
            await self.cog.on_message(message)
            await self.cog.on_message(message)
            await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertIn("成功", message.reply.call_args.args[0])

    async def test_markdown_and_long_output_fit_discord_limits(self):
        pages = translation_pages("**name**", "# heading\n" + "x" * 3000, "```\n" + "日" * 3000)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 1900 for page in pages))
        self.assertIn("```\n# heading", pages[0])
        for page in pages:
            self.assertIn(page.count("```"), (0, 2))
        self.assertIn("> \\`\\`\\`", "".join(pages))

    async def test_original_code_block_preserves_text_and_handles_embedded_fence(self):
        original = "Hello **everyone**!\n  keep spaces\n<@123> & 日本語"
        pages = translation_pages("name", original, "こんにちは")
        self.assertEqual(len(pages), 1)
        self.assertIn(f"```\n{original}\n```", pages[0])
        dangerous = translation_pages("name", "before\n```python\nprint(1)\n```\nafter", "訳")
        self.assertEqual(dangerous[0].count("```"), 2)
        self.assertIn("``\u200b`python", dangerous[0])


    async def test_text_targets_persist_and_survive_vc_departure(self):
        self.settings.configure(4, 10, {2, 5}, True, text_users={5})
        self.assertEqual(self.settings.text_users(4, 10), {5})
        restored = TranslationSettings(self.settings.path)
        self.assertEqual(restored.text_users(4, 10), {5})
        self.assertEqual(restored.users(4, 10), {2, 5})
        self.assertTrue(self.settings.remove_user(4, 2))
        self.assertEqual(self.settings.users(4, 10), {5})
        self.assertFalse(self.settings.remove_user(4, 5))
        self.assertEqual(self.settings.users(4, 10), {5})

    async def test_text_marks_cleared_on_reconfigure_and_change(self):
        self.settings.configure(4, 10, {5}, True, text_users={5})
        self.settings.configure(4, 10, {5}, True)
        self.assertEqual(self.settings.text_users(4, 10), set())
        self.settings.configure(4, 10, {5}, True, text_users={5})
        self.settings.change(4, 10, False, [5])
        self.assertEqual(self.settings.text_users(4, 10), set())
        self.assertEqual(self.settings.users(4, 10), set())

    async def test_legacy_file_loads_without_text_targets(self):
        self.settings.configure(4, 10, {2}, True)
        raw = self.settings.path.read_text(encoding="utf-8")
        self.assertIn('"version": 4', raw)
        self.assertEqual(TranslationSettings(self.settings.path).text_users(4, 10), set())


class TextTargetTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = TranslationSettings(Path(self.directory.name) / "settings.json")
        self.client = SimpleNamespace(translate=AsyncMock(return_value="こんにちは"), close=AsyncMock())
        self.controller = SimpleNamespace(set_enabled=AsyncMock())
        self.cog = TranslationCog(None, self.settings, self.client, self.controller, TalkLog(None, None),
                       SessionStore(Path(self.directory.name) / 'sessions'))
        self.cog.backend_ready = True
        self.whitelist = patch.object(self.cog.config, "allowed_channel_ids", {10})
        self.whitelist.start()
        self.addCleanup(self.whitelist.stop)
        self.owner = patch.object(self.cog.config, "owner_id", 1)
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.member = SimpleNamespace(id=2, bot=False, display_name="Somebody", voice=SimpleNamespace(channel=SimpleNamespace(id=3, name="General")))
        self.writer = SimpleNamespace(id=5, bot=False, display_name="Writer", voice=None)
        self.guild = SimpleNamespace(id=4, get_member=lambda uid: {2: self.member, 5: self.writer}.get(uid),
                                     voice_channels=[SimpleNamespace(members=[self.member])], stage_channels=[])
        self.member.guild = self.guild
        self.writer.guild = self.guild

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def message(self, author):
        return SimpleNamespace(id=100, guild=self.guild, channel=SimpleNamespace(id=10), author=author,
                               content="Hello", clean_content="Hello", webhook_id=None, reply=AsyncMock())

    def interaction(self, uid=1):
        return SimpleNamespace(user=SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=False)),
                               guild=self.guild, guild_id=4, channel_id=10,
                               response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    def history_channel(self, authors):
        async def history(limit=20):
            for author in authors[:limit]:
                yield SimpleNamespace(author=author, webhook_id=None)
        return SimpleNamespace(id=10, history=history)

    async def test_non_vc_text_target_is_translated(self):
        self.settings.configure(4, 10, {5}, True, text_users={5})
        await self.cog.cog_load()
        message = self.message(self.writer)
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.client.translate.assert_awaited_once_with("Hello")
        message.reply.assert_awaited_once()

    async def test_non_vc_target_without_text_mark_stays_ignored(self):
        self.settings.configure(4, 10, {5}, True)
        await self.cog.cog_load()
        await self.cog.on_message(self.message(self.writer))
        self.assertTrue(self.cog.queue.empty())

    async def test_vc_target_warns_when_backend_down(self):
        self.settings.configure(4, 10, {2}, True)
        self.cog.backend_ready = False
        message = self.message(self.member)
        await self.cog.on_message(message)
        self.assertTrue(self.cog.queue.empty())
        self.assertIn("停止", message.reply.call_args.args[0])

    async def test_panel_lists_recent_posters_and_marks_text_targets(self):
        self.guild.get_member = lambda uid: {2: self.member, 5: self.writer}.get(uid)
        interaction = self.interaction()
        interaction.channel = self.history_channel([self.writer, self.member])
        await TranslationCog.translate.callback(self.cog, interaction)
        view = interaction.followup.send.call_args.kwargs["view"]
        self.assertEqual(view.text_ids, {5})
        by_value = {o.value: o for o in view.selector.options}
        self.assertIn("5", by_value)
        self.assertIn("テキスト投稿", by_value["5"].description)
        self.assertIn("General", by_value["2"].description)
        view.selector._values = ["5"]
        await view.selector.callback(interaction)
        view.enabled_select._values = ["on"]
        await view.enabled_select.callback(interaction)
        await view.apply_button.callback(interaction)
        self.assertEqual(self.settings.users(4, 10), {5})
        self.assertEqual(self.settings.text_users(4, 10), {5})
        self.assertTrue(view.finished)

    async def test_panel_rejects_unknown_non_vc_user(self):
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        stranger = SimpleNamespace(id=9, bot=False, display_name="Stranger", voice=None)
        error = self.cog.validate(interaction, True, [stranger], view.text_ids)
        self.assertIn("直近の投稿者", error)
        writer_error = self.cog.validate(
            interaction, True, [self.writer], view.text_ids | {5})
        self.assertIsNone(writer_error)

    async def test_apply_resolves_members_from_panel_without_cache(self):
        self.guild.get_member = lambda uid: None
        interaction = self.interaction()
        interaction.channel = self.history_channel([self.writer])
        await TranslationCog.translate.callback(self.cog, interaction)
        view = interaction.followup.send.call_args.kwargs["view"]
        self.assertEqual(view.text_ids, {5})
        view.selector._values = ["5"]
        await view.selector.callback(interaction)
        view.enabled_select._values = ["on"]
        await view.enabled_select.callback(interaction)
        await view.apply_button.callback(interaction)
        self.assertTrue(view.finished)
        self.assertEqual(self.settings.users(4, 10), {5})
        self.assertEqual(self.settings.text_users(4, 10), {5})

    async def test_apply_with_gone_user_shows_reselect_error(self):
        self.settings.configure(4, 10, {9}, True)
        self.guild.get_member = lambda uid: None
        interaction = self.interaction()
        view = TranslationView(self.cog, interaction)
        self.assertEqual(view.users, {9})
        view.enabled_select._values = ["on"]
        await view.enabled_select.callback(interaction)
        await view.apply_button.callback(interaction)
        self.assertFalse(view.finished)
        sent = interaction.response.send_message.call_args.args[0]
        self.assertIn("見つからない", sent)
        self.assertEqual(self.settings.users(4, 10), {9})

    async def test_vc_departure_keeps_text_target_and_model_running(self):
        self.settings.configure(4, 10, {2, 5}, True, text_users={5})
        self.writer.voice = SimpleNamespace(channel=SimpleNamespace(id=3, name="General"))
        before = SimpleNamespace(channel=self.writer.voice.channel)
        self.writer.voice = None
        await self.cog.on_voice_state_update(self.writer, before, SimpleNamespace(channel=None))
        self.assertEqual(self.settings.users(4, 10), {2, 5})
        self.controller.set_enabled.assert_not_awaited()
        await self.cog.cog_load()
        await self.cog.on_message(self.message(self.writer))
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.client.translate.assert_awaited_once_with("Hello")


class TranslationClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_contract_and_output_validation(self):
        requests = []
        outputs = [
            {"choices": [{"message": {"content": "こんにちは"}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": "途中"}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": " "}, "finish_reason": "stop"}]},
        ]
        async def handler(request):
            requests.append((request.headers.get("Authorization"), await request.json()))
            return web.json_response(outputs.pop(0))
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("test-key\n")
            client = TranslationClient(f"http://127.0.0.1:{port}", str(key))
            try:
                self.assertEqual(await client.translate("Hello"), "こんにちは")
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        await client.translate("Hello")
                self.assertEqual(requests[0][0], "Bearer test-key")
                self.assertEqual(requests[0][1]["messages"][1]["content"], "Hello")
                self.assertFalse(requests[0][1]["stream"])
            finally:
                await client.close()
                await runner.cleanup()
