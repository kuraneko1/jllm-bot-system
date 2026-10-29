# ruff: noqa: E501
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.conversation.conversation import (
    ConversationCancelled,
    conversation_pages,
    reply_tree,
)
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.talk_log import TalkLog
from jllm_plugins.conversation.web_content import WebContentError
from jllm_plugins.llm.translation import TranslationSettings
from jllm_plugins.llm.translation_control import TranslationControlError
from jllm_plugins.translation.cog import TranslationCog, TranslationView


class FakeClient:
    # 分割しきい値は文脈長に対する割合で決まる（ローカルの llama.cpp 相当）。
    context_window = 4096

    def __init__(self):
        self.requests = []
        self.models = []
        self.translate = AsyncMock()
        self.close = AsyncMock()

    async def prompt_tokens(self, messages):
        return sum(len(m['content']) for m in messages) // 2

    async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
        self.requests.append(messages)
        self.models.append(model)
        return '資料の重要な点をまとめた回答です。'


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = TranslationSettings(Path(self.temp.name) / 'settings.json')
        self.client = FakeClient()
        self.controller = SimpleNamespace(set_enabled=AsyncMock())
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.cog = TranslationCog(self.bot, self.settings, self.client, self.controller,
                                  TalkLog(None, None), SessionStore(Path(self.temp.name) / 'sessions'))
        self.member = SimpleNamespace(id=2, display_name='User', bot=False, voice=None)
        self.guild = SimpleNamespace(id=1, voice_channels=[], stage_channels=[], get_member=lambda uid:self.member)
        self.history = []
        self.channel = SimpleNamespace(id=10, history=self.history_iter, fetch_message=AsyncMock())
        self.channel.typing = lambda: NullContext()
        patcher = patch.object(self.cog.config, 'allowed_channel_ids', {10})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings.configure(1, 10, set(), False, talk=True)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def history_iter(self, **kwargs):
        for message in self.history:
            yield message

    def message(self, mid=100, text='<@99> こんにちは', parent=None, bot=False):
        author = SimpleNamespace(id=99, bot=True, display_name='Bot') if bot else self.member
        reference = SimpleNamespace(message_id=parent.id, channel_id=10, resolved=parent) if parent else None
        return SimpleNamespace(id=mid, content=text, clean_content=text, author=author, guild=self.guild,
                               channel=self.channel, reference=reference, mentions=[self.bot.user] if '<@99>' in text else [],
                               webhook_id=None, attachments=[], reply=AsyncMock())

    async def test_open_channel_accepts_talk_configuration_without_whitelist(self):
        with patch.object(self.cog.config, 'allowed_channel_ids', {99}), \
             patch.object(self.cog.config, "talk_open_channels", frozenset({10})):
            self.assertIsNone(self.cog.validate(SimpleNamespace(channel_id=10), False, set()))
            denied = SimpleNamespace(channel_id=98)
            self.assertIn("許可チャンネル", self.cog.validate(denied, False, set()))

    async def test_mention_responds_without_vc_and_without_translation(self):
        message = self.message()
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_awaited_once()
        self.assertIn('こんにちは', self.client.requests[-1][1]['content'])
        self.client.translate.assert_not_awaited()
        self.assertFalse(message.reply.call_args.kwargs['allowed_mentions'].everyone)

    async def test_a_bare_miq_post_gets_no_talk_reply(self):
        message = self.message(mid=150, text='miq')
        await self.cog.on_message(message)
        await asyncio.sleep(0.05)
        message.reply.assert_not_awaited()
        self.assertEqual(self.client.requests, [])

    async def test_injected_miq_words_control_conversation_exclusion(self):
        from jllm_plugins.miq.cog import MiqCog, MiqConfig

        self.cog.miq = MiqCog(self.bot, config=MiqConfig(words=("quote",)))
        self.cog.enqueue_talk = AsyncMock()
        await self.cog.on_message(self.message(mid=152, text="quote"))
        self.cog.enqueue_talk.assert_not_awaited()
        await self.cog.on_message(self.message(mid=153, text="miq"))
        self.cog.enqueue_talk.assert_awaited_once()
        self.cog.enqueue_talk.reset_mock()
        self.cog.miq = None
        await self.cog.on_message(self.message(mid=154, text="quote"))
        self.cog.enqueue_talk.assert_awaited_once()

    async def test_injected_kitasan_can_be_disabled_or_customize_trigger(self):
        from jllm_plugins.conversation.kitasan import KitasanService

        self.cog.enqueue_talk = AsyncMock()
        self.cog.kitasan = KitasanService(self.client, words=("summary",), enabled=True)
        self.cog.kitasan.handle = AsyncMock()
        await self.cog.on_message(self.message(mid=155, text="summary"))
        self.cog.kitasan.handle.assert_awaited_once()
        self.cog.enqueue_talk.assert_not_awaited()
        await self.cog.on_message(self.message(mid=156, text="今北産業"))
        self.cog.enqueue_talk.assert_awaited_once()
        self.cog.enqueue_talk.reset_mock()
        self.cog.kitasan.enabled = False
        await self.cog.on_message(self.message(mid=157, text="summary"))
        self.cog.enqueue_talk.assert_awaited_once()
        self.cog.enqueue_talk.reset_mock()
        self.cog.kitasan = None
        await self.cog.on_message(self.message(mid=158, text="今北産業"))
        self.cog.enqueue_talk.assert_awaited_once()

    async def test_miq_with_extra_text_still_talks(self):
        message = self.message(mid=151, text='<@99> miq ってなに？')
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_awaited_once()
        self.assertTrue(self.client.requests)

    async def test_an_ignored_user_gets_no_reply(self):
        from jllm_plugins.restrictions.service import UserRestrictions

        store = UserRestrictions(Path(self.temp.name) / 'restrictions.json')
        store.set_flag(1, 2, 'ignore', True)
        self.cog.restrictions = store

        message = self.message(mid=160, text='<@99> こんにちは')
        await self.cog.on_message(message)
        await asyncio.sleep(0.05)

        message.reply.assert_not_awaited()
        self.assertEqual(self.client.requests, [])

    async def test_any_reply_to_a_miq_image_is_ignored(self):
        """miq が作った画像への返信には、内容によらず返事をしない。"""

        image = self.message(mid=200, text='', bot=True)
        image.attachments = [SimpleNamespace(filename='miq-123.png')]

        for text in ('miq', 'これ草', '<@99> どう思う？', '続きを話そう jbot'):
            with self.subTest(text=text):
                message = self.message(mid=201, text=text, parent=image)
                await self.cog.on_message(message)
                await asyncio.sleep(0.05)
                message.reply.assert_not_awaited()

        self.assertEqual(self.client.requests, [])

    async def test_a_reply_to_a_normal_bot_post_still_responds(self):
        parent = self.message(mid=210, text='<@99> 元の会話', bot=True)
        message = self.message(mid=211, text='続きです', parent=parent)
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_awaited_once()

    async def test_bot_reply_responds_and_other_posts_do_not(self):
        # 本人がメンションで会話を始める（回答には message_id が振られる）
        start = self.message(90, '<@99> こんにちは')
        start.reply = AsyncMock(return_value=SimpleNamespace(id=101))
        await self.cog.on_message(start)
        await asyncio.wait_for(self.cog.queue.join(), 1)

        # Botの回答への返信は、セッションに保存した履歴で続く
        bot_reply = self.message(101, '前の回答', bot=True)
        message = self.message(100, '短くして', bot_reply)
        self.history = [bot_reply]
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        prompt = self.client.requests[-1][1]['content']
        self.assertIn('このセッションの履歴', prompt)
        self.assertIn('こんにちは', prompt)

        normal = self.message(102, '普通の投稿')
        await self.cog.on_message(normal)
        human_reply = self.message(103, '人へのリプ', self.message(30, '別の話'))
        await self.cog.on_message(human_reply)
        self.assertTrue(self.cog.queue.empty())

    def other_member(self):
        other = SimpleNamespace(id=3, display_name='Other', bot=False, voice=None)
        self.guild.get_member = lambda uid: other if uid == 3 else self.member
        return other

    def from_other(self, text, mid, parent=None):
        message = self.message(mid, text, parent)
        message.author = self.other_member()
        return message

    async def test_other_member_reply_forks_and_mention_is_ignored(self):
        # 本人が会話を始める（Botの回答が message_id 101 で登録される）
        start = self.message(90, '<@99> 最初の呼び出し')
        start.reply = AsyncMock(return_value=SimpleNamespace(id=101))
        await self.cog.on_message(start)
        await asyncio.wait_for(self.cog.queue.join(), 1)

        bot_reply = self.message(101, '前の回答', bot=True)
        self.history = [bot_reply]
        session = self.cog.sessions.message(101)["session_id"]

        # 別の人の返信は「その時点のフォーク」として答える
        other_reply = self.from_other('横から失礼', 200, bot_reply)
        other_reply.reply = AsyncMock(return_value=SimpleNamespace(id=201))
        await self.cog.on_message(other_reply)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        fork = self.cog.sessions.message(201)["session_id"]
        self.assertNotEqual(fork, session)
        self.assertEqual(self.cog.sessions.session(fork)["parent_session"], session)
        self.assertEqual(self.cog.sessions.session(fork)["parent_user"], "3")

        # 既定（open）では、第三者のメンションにも自分の会話として応答する
        stranger = self.message(202, '<@99> 割り込み質問')
        stranger.author = SimpleNamespace(id=4, display_name='Stranger', bot=False, voice=None)
        stranger.reply = AsyncMock(return_value=SimpleNamespace(id=207))
        await self.cog.on_message(stranger)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertIsNotNone(self.cog.sessions.message(207))

        # session ゲートにすると、会話を持たない人は無視される（以前の挙動）
        with patch.object(self.cog.config, "talk_mention_gate", "session"):
            ignored = self.message(208, '<@99> 割り込み質問2')
            ignored.author = SimpleNamespace(id=5, display_name='Stranger2', bot=False, voice=None)
            await self.cog.on_message(ignored)
            self.assertTrue(self.cog.queue.empty())

        # 会話を持っている人（フォークした人）のメンションは自分の会話として通る
        mentioned = self.from_other('<@99> 続きを聞きたい', 203)
        mentioned.reply = AsyncMock(return_value=SimpleNamespace(id=204))
        await self.cog.on_message(mentioned)
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertIsNotNone(self.cog.sessions.message(204))

        # TALK_OPEN_CHANNELS のチャンネルでは、第三者のメンションでも普通に通る
        open_call = self.message(205, '<@99> 開いたチャンネルから')
        open_call.author = SimpleNamespace(id=4, display_name='Stranger', bot=False, voice=None)
        open_call.reply = AsyncMock(return_value=SimpleNamespace(id=206))
        with patch.object(self.cog.config, 'talk_open_channels', frozenset({10})):
            await self.cog.on_message(open_call)
            await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertIsNotNone(self.cog.sessions.message(206))

        entry = self.cog.talk_log.channel_stats(1, 10)
        self.assertEqual(entry["total"], 5)     # 本人・フォーク・続き・開いた・第三者の割り込み
        self.assertEqual(entry["ignored"], 1)   # session ゲートでの無視

    async def test_call_suffix_invokes_and_is_stripped(self):
        with patch.object(self.cog.config, "talk_call_suffix", "jbot"):
            await self.cog.on_message(self.message(300, 'これ要約して jbot'))
            await asyncio.wait_for(self.cog.queue.join(), 1)
            self.assertEqual(self.cog.talk_partner(10), 2)
            prompt = self.client.requests[-1][1]['content']
            self.assertIn('これ要約して', prompt)
            self.assertNotIn('jbot', prompt)

            # 語尾の合図がなければ、返信でもない投稿には反応しない
            await self.cog.on_message(self.message(301, 'ただの雑談'))
            self.assertTrue(self.cog.queue.empty())

            # 明示的な合図は会話中でも相手を交代できる（横槍メンションとは違う）
            await self.cog.on_message(self.from_other('私も聞きたい jbot', 302))
            await asyncio.wait_for(self.cog.queue.join(), 1)
            self.assertEqual(self.cog.talk_partner(10), 3)

    async def test_talk_off_forgets_the_partner(self):
        await self.cog.on_message(self.message(90, '<@99> こんにちは'))
        await asyncio.wait_for(self.cog.queue.join(), 1)
        self.assertEqual(self.cog.talk_partner(10), 2)
        await self.cog.configure(1, 10, set(), False, talk=False)
        self.assertNotIn(10, self.cog.talk_owner)

    async def test_reply_tree_includes_branches_and_nested_replies_only(self):
        root = self.message(10, '元の文章')
        branch = self.message(20, 'リプA', root)
        nested = self.message(30, 'Aへのリプ', branch)
        sibling = self.message(40, 'リプB', root)
        unrelated = self.message(50, '関係のない投稿')
        self.history = [unrelated, sibling, nested, branch, root]
        request = self.message(100, '<@99> この会話を要約して', nested)
        records, scope = await reply_tree(request, 99, lambda:True)
        self.assertEqual([r['id'] for r in records], ['10', '20', '30', '40'])
        self.assertIn('300', scope)

    async def test_off_during_inference_suppresses_output(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return '回答'
        self.client.complete = delayed
        message = self.message()
        await self.cog.on_message(message)
        await asyncio.wait_for(started.wait(), 1)
        await self.cog.configure(1, 10, set(), False)
        release.set()
        await asyncio.wait_for(self.cog.queue.join(), 1)
        message.reply.assert_not_awaited()
        self.assertFalse(self.settings.any_enabled())
        self.assertFalse(self.cog.talk_pending)

    async def test_long_attachment_uses_multiple_passes(self):
        message = self.message(text='<@99> 要約してください')
        message.attachments = [SimpleNamespace(filename='text.txt', size=16000,
                                               read=AsyncMock(return_value=('長い文章の内容。' * 2000).encode()))]
        result = await self.cog.conversation.respond(message, '要約してください', 99, lambda:True)
        self.assertGreater(len(self.client.requests), 2)
        self.assertIn('分割して要約', result)

    async def test_pending_request_limit_and_cancel_before_model_call(self):
        message = self.message()
        # Queue before worker runs, so a second request is rejected.
        await self.cog.on_message(message)
        second = self.message(101)
        await self.cog.on_message(second)
        self.assertIn('回答中', second.reply.call_args.args[0])
        await asyncio.wait_for(self.cog.queue.join(), 1)
        with self.assertRaises(ConversationCancelled):
            await self.cog.conversation.respond(message, '質問', 99, lambda:False)

    async def test_talk_persistence_and_last_model_lifecycle(self):
        restored = TranslationSettings(self.settings.path)
        self.assertEqual(restored.mode(1,10), 'talk')
        self.assertTrue(restored.any_enabled())
        self.settings.change(2, 11, True, [5])
        await self.cog.configure(1,10,set(),False)
        self.assertEqual(self.settings.mode(1,10),'off')
        self.assertTrue(self.settings.any_enabled())
        self.controller.set_enabled.assert_awaited_once_with(True)  # startup only

    async def test_panel_talk_mode_requires_no_vc_and_is_pending_until_confirm(self):
        self.settings.configure(1,10,set(),False)
        interaction = SimpleNamespace(user=self.member, guild=self.guild, guild_id=1, channel_id=10,
                                      response=SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock(), send_message=AsyncMock()),
                                      edit_original_response=AsyncMock())
        view = TranslationView(self.cog, interaction)
        view.enabled_select._values = ['talk']
        await view.enabled_select.callback(interaction)
        self.assertEqual(self.settings.mode(1,10),'off')
        self.assertFalse(view.apply_button.disabled)
        await view.apply_button.callback(interaction)
        self.assertEqual(self.settings.mode(1,10),'talk')
        self.assertTrue(view.finished)

    async def test_large_input_rejected_and_response_pages_bounded(self):
        message = self.message()
        with self.assertRaisesRegex(ValueError, '48,000'):
            await self.cog.conversation.respond(message, 'a' * 50000, 99, lambda:True)
        pages = conversation_pages('文章' * 3000)
        self.assertTrue(all(len(p) <= 1900 for p in pages))
        self.assertFalse(any('日本語訳' in p for p in pages))

    async def test_talk_start_failure_leaves_settings_off(self):
        self.settings.configure(1,10,set(),False)
        self.controller.set_enabled.side_effect = [TranslationControlError('空きVRAM不足'), None]
        with self.assertRaisesRegex(TranslationControlError, 'VRAM'):
            await self.cog.configure(1,10,set(),False,talk=True)
        self.assertEqual(self.settings.mode(1,10),'off')
        self.assertFalse(self.cog.backend_ready)

    async def test_modes_exclusive_and_old_format_migrates(self):
        await self.cog.configure(1,10,{2},True)
        self.assertEqual(self.settings.mode(1,10),'translate')
        self.assertFalse(self.settings.talk_channels)
        await self.cog.configure(1,10,set(),False,talk=True)
        self.assertFalse(self.settings.users(1,10))
        self.settings.path.write_text(json.dumps({'version':2,'targets':{'1:10':[2]},'show_original':{'1:10':False}}))
        restored=TranslationSettings(self.settings.path)
        self.assertEqual(restored.users(1,10),{2})
        self.assertFalse(restored.show_original(1,10))
        self.assertEqual(restored.mode(1,10),'translate')

    async def test_url_only_fetches_content_and_defaults_to_summary(self):
        fetch=AsyncMock(return_value={'url':'https://example.com/article','title':'記事の題名',
                                     'content':'## 計画\n\n予算は10万円。担当は佐藤さん。','truncated':False})
        self.cog.conversation.fetcher=SimpleNamespace(fetch=fetch)
        message=self.message(text='<@99> https://example.com/article')
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(),1)
        fetch.assert_awaited_once_with('https://example.com/article')
        prompt=self.client.requests[-1][1]['content']
        self.assertIn('予算は10万円',prompt)
        self.assertIn('Web本文',prompt)
        self.assertIn('日本語で要約',prompt)
        rendered=message.reply.call_args.args[0]
        self.assertIn('**参照元**',rendered)
        self.assertIn('https://example.com/article',rendered)
        self.assertFalse(rendered.startswith('> '))

    async def test_url_in_replied_post_is_fetched(self):
        fetch=AsyncMock(return_value={'url':'https://example.com','title':'ページ','content':'本文です。'*20,'truncated':True})
        self.cog.conversation.fetcher=SimpleNamespace(fetch=fetch)
        parent=self.message(30,'https://example.com')
        message=self.message(100,'<@99> 要約して',parent)
        self.history=[parent]
        answer=await self.cog.conversation.respond(message,'要約して',99,lambda:True)
        fetch.assert_awaited_once_with('https://example.com')
        self.assertIn('先頭12,000文字',answer)

    async def test_url_failure_is_reported_without_hallucinated_model_answer(self):
        self.cog.conversation.fetcher=SimpleNamespace(fetch=AsyncMock(side_effect=WebContentError('HTTP 403')))
        result=await self.cog.conversation.respond(self.message(),'https://example.com',99,lambda:True)
        self.assertIn('ページを取得できません',result)
        self.assertIn('HTTP 403',result)
        self.assertFalse(self.client.requests)

    async def test_url_limit_and_plain_posts_never_fetch(self):
        fetch=AsyncMock()
        self.cog.conversation.fetcher=SimpleNamespace(fetch=fetch)
        await self.cog.on_message(self.message(text='https://example.com'))
        fetch.assert_not_awaited()
        with self.assertRaisesRegex(ValueError,'3件'):
            await self.cog.conversation.respond(self.message(),' '.join(f'https://example.com/{i}' for i in range(4)),99,lambda:True)
        fetch.assert_not_awaited()

    async def test_markdown_headings_bold_and_code_fences_preserved_across_pages(self):
        text='## 要点\n- **予算：10万円**\n\n```python\n'+('print("test")\n'*300)+'```\n\n## 結論\n完了です。'
        pages=conversation_pages(text)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(len(page)<=1900 for page in pages))
        self.assertIn('## 要点',pages[0])
        self.assertIn('**予算：10万円**',pages[0])
        self.assertTrue(all(page.count('```')%2==0 for page in pages))
        self.assertIn('## 結論',pages[-1])

    async def test_off_during_web_fetch_suppresses_model_and_reply(self):
        started,release=asyncio.Event(),asyncio.Event()
        async def fetch(url):
            started.set()
            await release.wait()
            return {'url':url,'title':'記事','content':'本文'*100,'truncated':False}
        self.cog.conversation.fetcher=SimpleNamespace(fetch=fetch)
        message=self.message(text='<@99> https://example.com')
        await self.cog.on_message(message)
        await asyncio.wait_for(started.wait(),1)
        await self.cog.configure(1,10,set(),False)
        release.set()
        await asyncio.wait_for(self.cog.queue.join(),1)
        self.assertFalse(self.client.requests)
        message.reply.assert_not_awaited()


class NullContext:
    async def __aenter__(self):
        pass
    async def __aexit__(self, *args):
        pass
