# ruff: noqa: E501
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import MyTalkView, ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class MessageSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_does_not_interrupt_current_speech(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
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
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))

        async def send(text):
            message = SimpleNamespace(
                id=11,
                guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
                channel=SimpleNamespace(id=123), content=text, clean_content=text,
            )
            with patch.object(cog.config, 'allowed_channel_ids', {123}):
                await cog.on_message(message)

        for text in (';読まない', ':読まない', ';', ':', ';一行目\n二行目'):
            await send(text)
        player.skip.assert_not_called()
        player.enqueue.assert_not_called()
        cog._react_skip.assert_not_awaited()
        await send('次の投稿')
        player.enqueue.assert_called_once_with('次の投稿', None, 1.2, '11', '1:2', volume=1.0, effects=())
        await send('s')
        player.skip.assert_not_called()
        player.skip_current_post.assert_called_once()
        cog._react_skip.assert_awaited_once()

    async def test_a_bare_miq_post_is_not_read_aloud(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_v100 = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1, enqueue=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
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
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))

        async def send(text):
            message = SimpleNamespace(
                id=21, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
                channel=SimpleNamespace(id=123), content=text, clean_content=text,
                mentions=[], reference=None,
            )
            with patch.object(cog.config, 'allowed_channel_ids', {123}):
                await cog.on_message(message)

        for text in ('miq', 'MIQ', ' miq '):
            await send(text)
        player.enqueue.assert_not_called()

        # 「miq」を含む普通の文章は今までどおり読む
        await send('miq ってなに？')
        player.enqueue.assert_called_once()

    async def test_a_bare_kitasan_post_is_not_read_aloud(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_v100 = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1, enqueue=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
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
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))

        async def send(text):
            message = SimpleNamespace(
                id=31, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
                channel=SimpleNamespace(id=123), content=text, clean_content=text,
                mentions=[], reference=None,
            )
            with patch.object(cog.config, 'allowed_channel_ids', {123}):
                await cog.on_message(message)

        for text in ('今北産業', '  今北産業  ', '今北産業\n'):
            await send(text)
        player.enqueue.assert_not_called()

        # 「今北産業」を含む普通の文章は今までどおり読む
        await send('今北産業 ってなに？')
        player.enqueue.assert_called_once()

    async def test_inline_speed_assigns_per_piece_with_post_tag(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
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
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        message = SimpleNamespace(
            id=12,
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content='A f2 B', clean_content='A f2 B',
        )
        with patch.object(cog.config, 'allowed_channel_ids', {123}):
            await cog.on_message(message)
        self.assertEqual(player.enqueue.call_count, 2)
        player.enqueue.assert_any_call('A', None, 2.0, '12', '1:2', volume=1.0, effects=())
        player.enqueue.assert_any_call('B', None, 1.2, '12', '1:2', volume=1.0, effects=())

    async def test_trailing_s_clears_then_plays_text(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
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
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        message = SimpleNamespace(
            id=13,
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content='hello s', clean_content='hello s',
        )
        with patch.object(cog.config, 'allowed_channel_ids', {123}):
            await cog.on_message(message)
        player.skip.assert_called_once()
        player.enqueue.assert_called_once_with('hello', None, 1.2, '13', '1:2', volume=1.0, effects=())
        cog._react_skip.assert_awaited_once()


class SpeakerSelectionTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, speakers, selected=None):
        cog = ReaderCog.__new__(ReaderCog)
        cog.user_voices = {}
        cog.forced_voices = {}
        cog.voice_volumes = VoiceVolumes()
        cog.voice_speeds = VoiceSpeeds()
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        if selected:
            cog.user_voices[cog._user_key(1, 2)] = selected
        cog.players = {1: SimpleNamespace(speaker_id=speakers[0][0], skip_users=set())}
        cog._save_user_voices = Mock()
        return cog

    async def test_selection_updates_dropdown_and_saved_display(self):
        speakers = [(10, 'Clone First'), (30, 'Clone Second')]
        cog = self.make_cog(speakers, 'Clone First')
        view = MyTalkView(cog, guild_id=1, user_id=2, speakers=speakers)
        interaction = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(
            edit_message=AsyncMock(), send_message=AsyncMock()))

        for value in ('30', 'default', '30'):
            view.speaker_select._values = [value]
            await view.on_select(interaction)
            self.assertEqual(
                [o.value for o in view.speaker_select.options if o.default], [value]
            )
            kwargs = interaction.response.edit_message.call_args.kwargs
            self.assertIs(kwargs['view'], view)
            self.assertIn(view._option_label(value), kwargs['embed'].fields[-1].value)
        # Selection remains pending until confirmation.
        self.assertEqual(cog.user_voices[cog._user_key(1, 2)], 'Clone First')
        await view.on_apply(interaction)
        self.assertEqual(cog.user_voices[cog._user_key(1, 2)], 'Clone Second')
        reopened = MyTalkView(cog, guild_id=1, user_id=2, speakers=speakers)
        self.assertEqual([o.value for o in reopened.speaker_select.options if o.default], ['30'])
        self.assertEqual(reopened._embed().fields[0].value, 'Clone Second')

    async def test_current_voice_beyond_dropdown_limit_is_selected(self):
        speakers = [(i * 2, f'Clone Voice {i}') for i in range(40)]
        cog = self.make_cog(speakers, speakers[-1][1])
        view = MyTalkView(cog, guild_id=1, user_id=2, speakers=speakers)
        self.assertLessEqual(len(view.speaker_select.options), 25)
        self.assertEqual([o.value for o in view.speaker_select.options if o.default], ['78'])
        self.assertEqual(view._embed().fields[0].value, speakers[-1][1])


class FlattenSpeakersTests(unittest.IsolatedAsyncioTestCase):
    """Web UI（話者クローン）で無効にした話者は /mytalk の選択肢にも出さない。"""

    def make_cog(self, speakers, disabled=None):
        cog = ReaderCog.__new__(ReaderCog)
        tts = SimpleNamespace(speakers=AsyncMock(return_value=speakers))
        if disabled is not None:
            tts.disabled_voices = set(disabled)
        cog.tts = tts
        return cog

    async def test_disabled_voices_are_not_offered(self):
        speakers = [
            {
                "name": "自宅GPU（Irodori）",
                "styles": [
                    {"id": "トランプ1", "name": "トランプ1"},
                    {"id": "トランプ2", "name": "トランプ2"},
                ],
            }
        ]
        cog = self.make_cog(speakers, disabled={"トランプ2"})

        self.assertEqual(await cog._flatten_speakers(), [("トランプ1", "トランプ1")])

    async def test_router_catalog_toggle_hides_the_voice_from_mytalk(self):
        """Web UI で無効にした話者は、ルーター経由でも /mytalk から消える。"""

        import json
        import tempfile

        from jllm_plugins.tts_router.router import BackendSpec, TTSRouter
        from jllm_tts import TTSBackend, Voice

        class Backend(TTSBackend):
            def __init__(self):
                self.id = "ming"
                self.disabled_voices = set()

            async def list_voices(self):
                return [Voice(id="トランプ1", name="トランプ1"),
                        Voice(id="トランプ2", name="トランプ2")]

            async def synthesize(self, text, speaker_id, speed=1.0):
                return b""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disabled_voices.json"
            router = TTSRouter(
                [BackendSpec(id="ming", kind="openai", display="Ming")],
                env={"VOICE_DISABLED_PATH": str(path)},
                state_path=Path(directory) / "tts_state.json",
                builder=lambda spec: Backend(),
            )
            router.set_shared_voice_enabled("トランプ2", False)
            self.assertEqual(json.loads(path.read_text("utf-8")), ["トランプ2"])

            cog = ReaderCog.__new__(ReaderCog)
            cog.tts = router

            self.assertEqual(await cog._flatten_speakers(), [("トランプ1", "トランプ1")])

            router.set_shared_voice_enabled("トランプ2", True)
            self.assertEqual(
                [name for _, name in await cog._flatten_speakers()],
                ["トランプ1", "トランプ2"],
            )

    async def test_backend_without_disabled_set_is_tolerated(self):
        speakers = [{"name": "x", "styles": [{"id": 1, "name": "A"}]}]
        cog = self.make_cog(speakers)

        self.assertEqual(await cog._flatten_speakers(), [(1, "A")])
