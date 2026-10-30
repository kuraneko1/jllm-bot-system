# ruff: noqa: E501, E402
import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

WEBCHAT_PER_IP = 12
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.dashboard.dashboard import DashboardConfig, ReadingDashboard
from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.tts_router.router import BackendSpec, TTSRouter
from jllm_tts import TTSBackend, TTSBackendError, Voice

from tests.passkey_auth import login_witness, register_witness, witness_for


class FakeReader:
    def __init__(self, root):
        self.dictionary = SoundDictionary(str(root / "sounds.json"), str(root / "sounds"))
        self.media_dir = root / "media"
        self.media_dir.mkdir()
        self.media = {"hello": "hello.gif"}
        (self.media_dir / "hello.gif").write_bytes(b"GIF89a" + b"\x00" * 20)
        self.rebuilds = 0

    def _media_key(self, phrase):
        return next((key for key in self.media if key.casefold() == phrase.casefold()), None)

    def _save_media(self):
        (self.media_dir.parent / "media.json").write_text(json.dumps(self.media), encoding="utf-8")

    def _compress_gif(self, source, target):
        target.write_bytes(source.read_bytes())

    def _rebuild_sound_dashboard(self):
        self.rebuilds += 1


class FakeTTS:
    def __init__(self):
        self.calls = []

    def describe(self):
        return "irodori (fake)"


    async def speakers(self):
        return [{"name": "Irodori", "styles": [{"id": 1, "name": "none"}]}]

    async def synthesize(self, text, speaker_id, speed=1.0):
        self.calls.append((text, speaker_id, speed))
        return b"RIFF\x00\x00\x00\x00WAVEfake"


class ReadingDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.readings_path = root / "readings.json"
        self.readings_path.write_text(
            json.dumps({"DGXspark": "でぃーじーえっくす"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.password_path = root / "dashboard-auth.json"
        self.readings = ReadingDictionary(str(self.readings_path))
        self.reader = FakeReader(root)
        self.reader.dictionary.add("greeting", ["hello.wav", "hello2.wav"], author="Alice")
        (self.reader.dictionary.sounds_dir / "hello.wav").write_bytes(b"RIFFexample")
        (self.reader.dictionary.sounds_dir / "hello2.wav").write_bytes(b"RIFFexample2")
        self.tts = FakeTTS()
        self.sessions = SessionStore(root / "sessions")
        self.dashboard = ReadingDashboard(
            self.readings,
            self.tts,
            reader=self.reader,
            sessions=self.sessions,
            password_file=str(self.password_path),
            # 音量カーブの保存先を一時ディレクトリへ（既定 data/ を触らない）
            config=DashboardConfig(
                duck_curve_path=str(root / "duck_curve.json"),
                webchat_dir=str(root / "webchat"),
            ),
        )
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def login(self):
        witness = witness_for(self.dashboard)
        await register_witness(self.client, self.dashboard, witness)
        return await login_witness(self.client, self.dashboard, witness)

    async def test_llm_settings_auth_origin_redaction_and_update(self):
        class Client:
            def __init__(self):
                self.calls = []
            def admin_status(self):
                return {"backend": "openai", "backends": {"openai": {
                    "api_url": "https://user:secret@example.com/v1?token=secret", "model": "old",
                    "translate_model": "translator", "key_configured": True, "api_key": "secret"}}}
            async def admin_update(self, payload):
                self.calls.append(payload)
                return {"api_key": "secret"}
        fake = Client()
        self.dashboard.webchat_client = fake
        self.assertEqual((await self.client.get('/api/llm/settings')).status, 401)
        self.assertEqual((await self.client.put('/api/llm/settings')).status, 401)
        self.assertEqual((await self.client.post('/api/llm/active')).status, 401)
        await self.login()
        self.dashboard._passkey_origins = [(str(self.client.make_url('/')).rstrip('/'), 'localhost')]
        origin = self.dashboard._passkey_origins[0][0]
        response = await self.client.get('/api/llm/settings')
        self.assertEqual(response.status, 200)
        self.assertNotIn('secret', await response.text())
        self.assertEqual((await response.json())['backends']['openai']['api_url'], '')
        fake.admin_status = lambda: {'backend': 'openai', 'backends': {'openai': {'api_url': 'http://[::1]:8080/v1', 'model': 'old', 'translate_model': 't', 'key_configured': True}}}
        self.assertEqual((await (await self.client.get('/api/llm/settings')).json())['backends']['openai']['api_url'], 'http://[::1]:8080/v1')
        fake.admin_status = lambda: {'backend': 'openai', 'backends': {'openai': {'api_url': 'https://example.com/secret', 'key_configured': True}}}
        self.assertNotIn('secret', await (await self.client.get('/api/llm/settings')).text())
        payload = {'backend': 'openai', 'api_url': 'https://example.com/v1', 'model': 'new', 'translate_model': 't', 'api_key': ''}
        for headers in ({}, {'Origin': 'http://evil.example'}, {'Origin': origin.replace('http:', 'https:')}):
            result = await self.client.put('/api/llm/settings', json=payload, headers=headers)
            self.assertEqual(result.status, 403)
        self.assertFalse(fake.calls)
        result = await self.client.put('/api/llm/settings', json=payload, headers={'Origin': origin})
        self.assertEqual(result.status, 200)
        self.assertNotIn('secret', await result.text())
        self.assertNotIn('api_key', fake.calls[-1])
        payload['api_key'] = 'new-secret'
        result = await self.client.put('/api/llm/settings', json=payload, headers={'Origin': origin})
        self.assertEqual(result.status, 200)
        self.assertEqual(fake.calls[-1]['api_key'], 'new-secret')
        self.assertNotIn('new-secret', await result.text())
        result = await self.client.put('/api/llm/settings', json={'backend': 'openai', 'remove_api_key': True}, headers={'Origin': origin})
        self.assertEqual(result.status, 200)
        self.assertEqual(fake.calls[-1], {'backend': 'openai', 'api_key': ''})
        for bad in ({'backend': 'unknown'}, {'backend': []}, {'backend': {}}, {'backend': 'openai', 'api_url': 'https://a/?key=x'}, {'backend': 'openai', 'api_url': 'https://a/secret'}, {'backend': 'openai', 'model': 1}, {'backend': 'openai', 'remove_api_key': 'yes'}, {'backend': 'openai', 'remove_api_key': True, 'api_key': 'x'}):
            self.assertEqual((await self.client.put('/api/llm/settings', json=bad, headers={'Origin': origin})).status, 400)
        self.assertEqual((await self.client.post('/api/llm/active', json={'backend': 'local'})).status, 403)
        self.assertEqual((await self.client.post('/api/llm/active', json={'backend': []}, headers={'Origin': origin})).status, 400)
        result = await self.client.post('/api/llm/active', json={'backend': 'local'}, headers={'Origin': origin})
        self.assertEqual(result.status, 409)
        self.assertIn('安全性未確認', (await result.json())['error'])
        self.assertEqual((await self.client.post('/api/llm/active', json={'backend': 'atmos'}, headers={'Origin': origin})).status, 503)
        selected = []
        async def select(backend):
            selected.append(backend)
        fake.admin_select = select
        result = await self.client.post('/api/llm/active', json={'backend': 'atmos'}, headers={'Origin': origin})
        self.assertEqual(result.status, 200)
        self.assertEqual(selected, ['atmos'])
        fake.admin_status = lambda: {'backend': 'local', 'backends': {}}
        result = await self.client.post('/api/llm/active', json={'backend': 'openai'}, headers={'Origin': origin})
        self.assertEqual(result.status, 409)
        self.assertEqual(selected, ['atmos'])
        self.assertNotIn('secret', await result.text())
        async def fail(payload):
            raise RuntimeError('secret exception')
        fake.admin_update = fail
        with self.assertNoLogs('jllm_plugins.dashboard.dashboard', level='WARNING'):
            result = await self.client.put('/api/llm/settings', json=payload, headers={'Origin': origin})
            self.assertEqual(result.status, 503)
            self.assertNotIn('secret', await result.text())

    async def test_public_icons_are_served(self):
        icon = await self.client.get("/favicon.svg")
        self.assertEqual(icon.status, 200)
        self.assertIn("image/svg+xml", icon.headers["Content-Type"])
        self.assertIn("<svg", await icon.text())
        self.assertEqual((await self.client.get("/favicon.ico")).status, 200)
        touch = await self.client.get("/apple-touch-icon.png")
        self.assertEqual(touch.status, 200)
        self.assertEqual((await touch.read())[:8], b"\x89PNG\r\n\x1a\n")

    async def test_public_board_lists_dictionary_and_plays_registered_assets(self):
        for path in ('/api/readings', '/api/sounds', '/api/gifs'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['entries'])
        sound = await self.client.get('/api/sounds/file?phrase=greeting&file=hello.wav')
        self.assertEqual(await sound.read(), b'RIFFexample')
        gif = await self.client.get('/api/gifs/file?trigger=hello')
        self.assertEqual((await gif.read())[:6], b'GIF89a')
        # A file that exists but has not been approved/registered is never public.
        (self.reader.dictionary.sounds_dir / 'pending.wav').write_bytes(b'private')
        hidden = await self.client.get('/api/sounds/file?phrase=greeting&file=pending.wav')
        self.assertEqual(hidden.status, 404)

    async def test_voice_debug_reports_connection_state(self):
        """音が出ないときの切り分け用に、接続・DAVE・送受信の数値を返す。"""

        self.reader.players = {
            1: SimpleNamespace(voice_status=lambda: {
                "guild_id": "1", "connected": True, "dave": {"protocol_version": 1},
                "send": {"packets": 3}, "receive": {"packets": 9},
            }),
        }

        response = await self.client.get('/api/voice-debug')

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["watching"])
        self.assertEqual(body["guilds"][0]["send"]["packets"], 3)

        self.reader.players = {}

    async def test_every_admin_action_requires_passkey_session_even_on_public_board(self):
        for method, path in [
            ('post', '/api/readings'), ('put', '/api/readings'), ('delete', '/api/readings'),
            ('put', '/api/sounds'), ('delete', '/api/sounds'), ('post', '/api/sounds/upload'),
            ('put', '/api/gifs'), ('delete', '/api/gifs'), ('post', '/api/gifs/upload'),
            ('get', '/api/voices'), ('get', '/api/voice-library'), ('get', '/api/voice-catalog'),
            ('get', '/api/tts/backends'), ('post', '/api/tts/active'), ('post', '/api/tts/check'),
            ('post', '/api/tts/stream'), ('post', '/api/tts/failover'),
            ('put', '/api/voice-library'), ('put', '/api/voice-catalog'), ('post', '/api/voice-library/upload'),
            ('delete', '/api/voice-library'), ('post', '/api/generate'),
        ]:
            with self.subTest(method=method, path=path):
                response = await getattr(self.client, method)(path)
                self.assertEqual(response.status, 401)
        await self.login()
        await self.client.post('/api/logout')
        denied = await self.client.delete('/api/sounds', json={'phrase': 'greeting'})
        self.assertEqual(denied.status, 401)
        blocked = await self.client.post('/api/passkeys/register/options')
        self.assertEqual(blocked.status, 401)
        self.assertIn('greeting', self.reader.dictionary.entries)
        self.assertEqual((await self.client.get('/api/sounds')).status, 200)

    async def test_edit_reading_renames_without_overwriting_another_entry(self):
        await self.login()
        response = await self.client.put(
            "/api/readings", json={"original": "DGXspark", "trigger": "DGX Spark", "reading": "でぃーじー"},
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn("DGXspark", self.readings.entries)
        self.assertEqual(self.readings.apply("DGX Spark"), "でぃーじー")
        await self.client.post("/api/readings", json={"trigger": "other", "reading": "ほか"})
        collision = await self.client.put(
            "/api/readings", json={"original": "DGX Spark", "trigger": "other", "reading": "こわす"},
        )
        self.assertEqual(collision.status, 409)
        self.assertEqual(self.readings.entries["other"], "ほか")

    async def test_sound_dictionary_lists_files_and_edits_trigger_preserving_public_dashboard(self):
        await self.login()
        entries = await (await self.client.get("/api/sounds")).json()
        self.assertEqual(entries["entries"][0]["author"], "Alice")
        self.assertEqual(entries["entries"][0]["files"], ["hello.wav", "hello2.wav"])
        preview = await self.client.get("/api/sounds/file?phrase=greeting&file=hello.wav")
        self.assertEqual(preview.status, 200)
        self.assertEqual(await preview.read(), b"RIFFexample")
        blocked = await self.client.get("/api/sounds/file?phrase=greeting&file=../media/hello.gif")
        self.assertEqual(blocked.status, 404)
        rename = await self.client.put(
            "/api/sounds", json={"original": "greeting", "phrase": "hello world"},
        )
        self.assertEqual(rename.status, 200)
        self.assertEqual(self.reader.dictionary.entries["hello world"], ["hello.wav", "hello2.wav"])
        self.assertEqual(self.reader.dictionary.author_of("hello world"), "Alice")
        self.assertEqual(self.reader.rebuilds, 1)
        removed = await self.client.delete("/api/sounds", json={"phrase": "hello world"})
        self.assertEqual(removed.status, 200)
        self.assertTrue((self.reader.dictionary.sounds_dir / "hello.wav").exists())
        self.assertEqual(self.reader.rebuilds, 2)

    async def test_sound_upload_transcodes_and_registers_and_rejects_missing_auth(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 1600)
        def sound_form():
            form = FormData()
            form.add_field("phrase", "new sound")
            form.add_field("file", buffer.getvalue(), filename="sample.wav", content_type="audio/wav")
            return form
        denied = await self.client.post("/api/sounds/upload", data=sound_form())
        self.assertEqual(denied.status, 401)
        await self.login()
        response = await self.client.post("/api/sounds/upload", data=sound_form())
        self.assertEqual(response.status, 201)
        files = self.reader.dictionary.entries["new sound"].copy()
        self.assertEqual(len(files), 1)
        self.assertTrue((self.reader.dictionary.sounds_dir / files[0]).is_file())
        self.assertEqual(self.reader.rebuilds, 1)

        append = sound_form()
        append.add_field("append", "true")
        appended = await self.client.post("/api/sounds/upload", data=append)
        self.assertEqual(appended.status, 201)
        self.assertEqual(len(self.reader.dictionary.entries["new sound"]), 2)
        edit = await self.client.put(
            "/api/sounds", json={"original": "new sound", "phrase": "new sound", "files": files},
        )
        self.assertEqual(edit.status, 200)
        self.assertEqual(self.reader.dictionary.entries["new sound"], files)
        self.assertTrue((self.reader.dictionary.sounds_dir / (await appended.json())["file"]).exists())

    async def test_gif_dictionary_previews_uploads_renames_and_removes(self):
        await self.login()
        listed = await (await self.client.get("/api/gifs")).json()
        self.assertEqual(listed["entries"][0]["trigger"], "hello")
        preview = await self.client.get("/api/gifs/file?trigger=hello")
        self.assertEqual(preview.status, 200)
        self.assertEqual((await preview.read())[:6], b"GIF89a")
        gif = base64.b64decode("R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=")
        form = FormData()
        form.add_field("trigger", "new gif")
        form.add_field("file", gif, filename="sample.gif", content_type="image/gif")
        upload = await self.client.post("/api/gifs/upload", data=form)
        self.assertEqual(upload.status, 201)
        self.assertIn("new gif", self.reader.media)
        rename = await self.client.put(
            "/api/gifs", json={"original": "new gif", "trigger": "renamed gif"},
        )
        self.assertEqual(rename.status, 200)
        self.assertIn("renamed gif", self.reader.media)
        removed = await self.client.delete("/api/gifs", json={"trigger": "renamed gif"})
        self.assertEqual(removed.status, 200)
        self.assertNotIn("renamed gif", self.reader.media)

    async def test_dashboard_page_is_served_and_cross_origin_edits_are_blocked(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        page = await response.text()
        self.assertIn("<title>jllm voice</title>", page)
        self.assertIn("/api/generate", page)

        await self.login()
        response = await self.client.post(
            "/api/readings",
            json={"trigger": "blocked", "reading": "ぶろっく"},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn("blocked", self.readings.entries)

    async def test_authenticated_dashboard_can_view_add_and_delete_readings(self):
        public = await self.client.get("/api/readings")
        self.assertEqual(public.status, 200)

        response = await self.login()
        self.assertEqual(response.status, 200)

        response = await self.client.get("/api/readings")
        payload = await response.json()
        self.assertEqual(payload["entries"][0], {"trigger": "DGXspark", "reading": "でぃーじーえっくす"})

        response = await self.client.post(
            "/api/readings",
            json={"trigger": "arxiv", "reading": "あーかいぶ"},
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(self.readings.apply("arxiv"), "あーかいぶ")

        response = await self.client.delete(
            "/api/readings",
            json={"trigger": "arxiv"},
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn("arxiv", self.readings.entries)

    async def test_forwarded_https_sets_secure_session_cookie(self):
        witness = witness_for(self.dashboard)
        await register_witness(self.client, self.dashboard, witness)
        options = await (await self.client.post('/api/passkeys/login/options')).json()
        response = await self.client.post(
            '/api/passkeys/login/verify',
            json={'credential': witness.assertion(options['challenge'], self.dashboard._passkeys['user_id'])},
            headers={'X-Forwarded-Proto': 'https'},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies['reading_dashboard_session']['secure'])

    async def test_generate_applies_reading_dictionary_and_returns_audio(self):
        await self.login()
        response = await self.client.post(
            "/api/generate",
            json={"text": "DGXspark test", "speaker": 1, "speed": 1.25},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "audio/wav")
        self.assertEqual(await response.read(), b"RIFF\x00\x00\x00\x00WAVEfake")
        self.assertEqual(self.tts.calls, [("でぃーじーえっくす test", 1, 1.25)])

    async def test_passkey_survives_restart_without_password_file(self):
        witness = witness_for(self.dashboard)
        await register_witness(self.client, self.dashboard, witness)
        self.assertFalse(self.password_path.exists())
        restarted = ReadingDashboard(
            self.readings,
            self.tts,
            password_file=str(self.password_path),
        )
        client = TestClient(TestServer(restarted.app))
        await client.start_server()
        try:
            response = await login_witness(client, restarted, witness)
            self.assertEqual(response.status, 200)
            session = await (await client.get('/api/session')).json()
            self.assertTrue(session['authenticated'])
            self.assertTrue(session['passkey_registered'])
        finally:
            await client.close()


    async def test_sessions_are_public_but_names_are_anonymous(self):
        # 未ログインでも読める。ただし名前は匿名で、ID とアイコンは返さない。
        session, _ = self.sessions.resolve("1", "10", "2", "Aさん", None)
        self.sessions.append_user(session, 2, "Aさん", "最初の質問", "100", avatar="hash1")
        self.sessions.append_assistant(session, "最初の答え", "101")

        response = await self.client.get("/api/sessions")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["anonymous"])
        self.assertEqual(body["total"], 1)
        row = body["sessions"][0]
        self.assertTrue(row["parent_user_name"].startswith("ゲスト"))
        self.assertNotIn("Aさん", row["parent_user_name"])
        self.assertEqual(row["parent_user"], "")
        self.assertEqual(row["parent_user_avatar"], "")

        detail = await (await self.client.get(f"/api/sessions/{session}")).json()
        self.assertTrue(detail["anonymous"])
        turn = detail["turns"][0]
        self.assertTrue(turn["user_name"].startswith("ゲスト"))
        self.assertIsNone(turn["user_id"])
        self.assertEqual(detail["users"], {})
        # 会話の中身と時刻は伏せない
        self.assertEqual(turn["content"], "最初の質問")
        self.assertEqual(detail["turns"][1]["role"], "assistant")

        # 同じ人の匿名名は、一覧と中身で一致する
        self.assertEqual(turn["user_name"], row["parent_user_name"])

        # ログインすると本名とアイコンが出る
        await self.login()
        admin_detail = await (await self.client.get(f"/api/sessions/{session}")).json()
        self.assertFalse(admin_detail["anonymous"])
        self.assertEqual(admin_detail["turns"][0]["user_name"], "Aさん")
        self.assertEqual(admin_detail["turns"][0]["user_id"], "2")
        self.assertIn("cdn.discordapp.com", admin_detail["users"]["2"]["avatar_url"])

        admin_list = await (await self.client.get("/api/sessions")).json()
        self.assertFalse(admin_list["anonymous"])
        self.assertEqual(admin_list["sessions"][0]["parent_user_name"], "Aさん")


    async def test_session_list_shows_forks_and_turns(self):
        session, _ = self.sessions.resolve("1", "10", "2", "Aさん", None)
        self.sessions.update_settings(session, model="sarashina3-mini", system_prompt="人格")
        self.sessions.append_user(session, 2, "Aさん", "最初の質問", "100")
        self.sessions.append_assistant(session, "最初の答え", "101")
        fork, _ = self.sessions.resolve("1", "10", "3", "Bさん", "101")
        self.sessions.append_user(fork, 3, "Bさん", "横からの質問", "102")

        await self.login()

        listed = await self.client.get("/api/sessions")
        self.assertEqual(listed.status, 200)
        body = await listed.json()
        ids = [row["session_id"] for row in body["sessions"]]
        self.assertCountEqual(ids, [session, fork])
        newest = body["sessions"][0]
        self.assertEqual(newest["session_id"], fork)
        self.assertEqual(newest["parent_user_name"], "Bさん")
        self.assertEqual(newest["parent_session"], session)

        detail = await self.client.get(f"/api/sessions/{fork}")
        self.assertEqual(detail.status, 200)
        data = await detail.json()
        self.assertEqual(data["session"]["session_id"], fork)
        self.assertEqual(
            [(turn["role"], turn["content"]) for turn in data["turns"]],
            [("user", "最初の質問"), ("assistant", "最初の答え"), ("user", "横からの質問")],
        )
        self.assertEqual(data["turns"][0]["user_name"], "Aさん")

        missing = await self.client.get("/api/sessions/10-9999")
        self.assertEqual(missing.status, 404)

    async def test_session_detail_clips_long_turns(self):
        session, _ = self.sessions.resolve("1", "10", "2", "A", None)
        self.sessions.append_user(session, 2, "A", "あ" * 5000, "100")
        await self.login()
        data = await (await self.client.get(f"/api/sessions/{session}")).json()
        turn = data["turns"][0]
        self.assertEqual(len(turn["content"]), 2001)
        self.assertTrue(turn["truncated"])

    async def test_tts_api_reports_single_engine_when_not_multiplexed(self):
        await self.login()
        data = await (await self.client.get("/api/tts/backends")).json()
        self.assertFalse(data["router"])
        self.assertEqual(data["backends"], [])
        self.assertIn("irodori", data["describe"])
        denied = await self.client.post("/api/tts/active", json={"id": "spare"})
        self.assertEqual(denied.status, 400)

    async def test_sessions_api_without_store_reports_503(self):
        dashboard = ReadingDashboard(
            self.readings, self.tts, reader=self.reader, password_file=str(self.password_path),
        )
        self.assertIsNone(dashboard.sessions)


class FakeChatClient:
    """SarashinaClient.stream の代わり。トークンを少しずつ返す。"""

    def __init__(self, chunks=("こん", "にち", "は！"), error=None):
        self.chunks = list(chunks)
        self.error = error
        self.calls = []

    async def stream(self, messages, max_tokens=1200, model=None, **kwargs):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "model": model})

        if self.error is not None:
            raise self.error

        for chunk in self.chunks:
            yield chunk


class FakeChatTTS(TTSBackend):
    # 切替テスト用のTTS。fail を立てると接続できない状態になる。

    supports_voice_management = True

    def __init__(self, ident, *, fail=False):
        self.id = ident
        self.fail = fail
        self.disabled_voices = set()

    def describe(self):
        return f"fake({self.id})"

    async def list_voices(self):
        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

        return [Voice(id=self.id, name=self.id)]

    async def synthesize(self, text, speaker_id, speed=1.0):
        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

        return b"wav"

    async def health(self):
        if self.fail:
            raise TTSBackendError(f"{self.id} に接続できません")

    async def close(self):
        return None


class FakeBotUser:
    id = 1234
    name = "jllm-voice"
    display_name = "jllm-voice"

    class avatar:
        key = "a_deadbeef"


class FakeBot:
    user = FakeBotUser()


def parse_sse(body: str):
    """SSE の本文を (event, data) の並びにする。"""
    events = []

    for block in body.strip().split("\n\n"):
        name, data = None, None

        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])

        if name:
            events.append((name, data))

    return events


class WebchatApiTests(unittest.IsolatedAsyncioTestCase):
    """Web の AI チャット（管理者専用・Discord セッションとは別保存）。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.readings_path = root / "readings.json"
        self.readings_path.write_text(json.dumps({}), encoding="utf-8")
        self.password_path = root / "dashboard-auth.json"
        self.webchat = SessionStore(root / "webchat")
        self.sessions = SessionStore(root / "sessions")
        self.chat = FakeChatClient()
        self.dashboard = ReadingDashboard(
            ReadingDictionary(str(self.readings_path)),
            FakeTTS(),
            sessions=self.sessions,
            webchat=self.webchat,
            webchat_client=self.chat,
            password_file=str(self.password_path),
        )
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def login(self):
        witness = witness_for(self.dashboard)
        await register_witness(self.client, self.dashboard, witness)
        return await login_witness(self.client, self.dashboard, witness)

    async def new_session(self):
        response = await self.client.post("/api/webchat/sessions")
        self.assertEqual(response.status, 200)
        return (await response.json())["session_id"]

    async def send(self, session_id, text):
        response = await self.client.post(
            f"/api/webchat/sessions/{session_id}/stream", json={"text": text},
        )
        return response, parse_sse(await response.text())

    async def test_webchat_routes_are_admin_only(self):
        for method, path in [
            ("get", "/api/webchat/sessions"),
            ("post", "/api/webchat/sessions"),
            ("get", "/api/webchat/sessions/web-0001"),
            ("delete", "/api/webchat/sessions/web-0001"),
            ("post", "/api/webchat/sessions/web-0001/stream"),
        ]:
            with self.subTest(method=method, path=path):
                response = await getattr(self.client, method)(path)
                self.assertEqual(response.status, 401)

    async def test_webchat_streams_tokens_and_keeps_its_own_history(self):
        await self.login()
        session_id = await self.new_session()

        response, events = await self.send(session_id, "こんにちは")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual([name for name, _ in events], ["start", "delta", "delta", "delta", "done"])
        self.assertEqual("".join(data["text"] for name, data in events if name == "delta"), "こんにちは！")
        self.assertEqual(events[-1][1]["content"], "こんにちは！")
        self.assertEqual(events[0][1]["session_id"], session_id)
        # 会話モデルへは system プロンプトと今回の発言だけを渡す。
        sent = self.chat.calls[0]
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(sent["messages"][-1]["role"], "user")
        self.assertEqual(sent["messages"][-1]["content"], "こんにちは")

        detail = await (await self.client.get(f"/api/webchat/sessions/{session_id}")).json()
        self.assertEqual(detail["assistant"]["name"], "jllm-voice")
        self.assertEqual(detail["session"]["channel_name"], "Webチャット")
        self.assertEqual(
            [(turn["role"], turn["content"]) for turn in detail["turns"]],
            [("user", "こんにちは"), ("assistant", "こんにちは！")],
        )

        listed = await (await self.client.get("/api/webchat/sessions")).json()
        self.assertEqual([row["session_id"] for row in listed["sessions"]], [session_id])
        self.assertEqual(listed["assistant"]["name"], "jllm-voice")
        # Discord のセッション置き場は空のまま（混ざらない）。
        self.assertEqual(self.sessions.all_sessions(), [])

    async def test_existing_webchat_uses_leased_current_model(self):
        from contextlib import asynccontextmanager

        await self.login()
        self.chat.model = 'old-model'
        session_id = await self.new_session()
        saved_model = self.webchat.settings(session_id)['model']
        leased = FakeChatClient()
        leased.model = 'new-model'
        @asynccontextmanager
        async def lease():
            yield leased
        self.chat.lease = lease
        response, events = await self.send(session_id, '次の発言')
        self.assertEqual(response.status, 200)
        self.assertEqual(events[0][1]['model'], 'new-model')
        self.assertEqual(events[-1][1]['model'], 'new-model')
        self.assertIsNone(leased.calls[0]['model'])
        self.assertEqual(self.webchat.settings(session_id)['model'], saved_model)
        self.assertEqual(len(self.webchat.history(session_id)), 2)

    async def test_webchat_rejects_bad_input_and_unknown_sessions(self):
        await self.login()
        session_id = await self.new_session()

        missing = await self.client.post("/api/webchat/sessions/web-9999/stream", json={"text": "やあ"})
        self.assertEqual(missing.status, 404)
        empty = await self.client.post(f"/api/webchat/sessions/{session_id}/stream", json={"text": "  "})
        self.assertEqual(empty.status, 400)
        broken = await self.client.post(
            f"/api/webchat/sessions/{session_id}/stream", data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(broken.status, 400)
        too_long = await self.client.post(
            f"/api/webchat/sessions/{session_id}/stream", json={"text": "あ" * 5000},
        )
        self.assertEqual(too_long.status, 400)
        self.assertEqual(self.chat.calls, [])
        self.assertEqual((await self.client.get("/api/webchat/sessions/web-9999")).status, 404)
        self.assertEqual((await self.client.delete("/api/webchat/sessions/web-9999")).status, 404)

    async def test_webchat_reports_model_errors_without_saving_a_reply(self):
        await self.login()
        session_id = await self.new_session()
        self.chat.error = RuntimeError("boom")

        response, events = await self.send(session_id, "失敗して")

        self.assertEqual(response.status, 200)
        self.assertEqual([name for name, _ in events], ["start", "error"])
        self.assertIn("生成に失敗", events[-1][1]["message"])
        detail = await (await self.client.get(f"/api/webchat/sessions/{session_id}")).json()
        self.assertEqual([turn["role"] for turn in detail["turns"]], ["user"])

    async def test_webchat_without_a_model_reports_503(self):
        await self.login()
        session_id = await self.new_session()
        self.dashboard.webchat_client = None
        response = await self.client.post(
            f"/api/webchat/sessions/{session_id}/stream", json={"text": "やあ"},
        )
        self.assertEqual(response.status, 503)

    async def test_webchat_is_rate_limited_per_client(self):
        await self.login()
        session_id = await self.new_session()

        for _ in range(WEBCHAT_PER_IP):
            response, _ = await self.send(session_id, "やあ")
            self.assertEqual(response.status, 200)

        blocked, _ = await self.send(session_id, "やあ")
        self.assertEqual(blocked.status, 429)
        self.assertEqual(len(self.chat.calls), WEBCHAT_PER_IP)

    async def test_webchat_deletes_a_conversation_and_its_file(self):
        await self.login()
        session_id = await self.new_session()
        await self.send(session_id, "やあ")

        removed = await self.client.delete(f"/api/webchat/sessions/{session_id}")
        self.assertEqual(removed.status, 200)
        self.assertEqual((await removed.json())["deleted"], session_id)
        self.assertEqual((await self.client.get(f"/api/webchat/sessions/{session_id}")).status, 404)
        self.assertEqual(self.webchat.all_sessions(), [])
        left = sorted(path.name for path in (Path(self.temp_dir.name) / "webchat").glob("*.sqlite"))
        self.assertEqual(left, ["index.sqlite"])

    async def test_webchat_uses_the_bot_avatar_when_the_bot_is_known(self):
        await self.login()
        self.dashboard.reader = type("R", (), {"bot": FakeBot()})()

        listed = await (await self.client.get("/api/webchat/sessions")).json()

        self.assertEqual(listed["assistant"]["name"], "jllm-voice")
        self.assertEqual(
            listed["assistant"]["avatar_url"],
            "https://cdn.discordapp.com/avatars/1234/a_deadbeef.gif?size=64",
        )



class TtsApiTests(unittest.IsolatedAsyncioTestCase):
    """読み上げエンジンの切替API（多重化しているときだけ）。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.readings_path = root / "readings.json"
        self.readings_path.write_text(json.dumps({}), encoding="utf-8")
        self.password_path = root / "dashboard-auth.json"
        self.backends = {"home": FakeChatTTS("home"), "spare": FakeChatTTS("spare")}
        self.router = TTSRouter(
            [BackendSpec(id="home", kind="fake", display="自宅", options={"url": "http://home"}),
             BackendSpec(id="spare", kind="fake", display="予備", options={"url": "http://spare"})],
            env={},
            state_path=root / "tts_state.json",
            builder=lambda spec: self.backends[spec.id],
        )
        # 無効話者の共通ファイルは本物（data/disabled_voices.json）を触らない
        self.router.env = {"VOICE_DISABLED_PATH": str(root / "disabled_voices.json")}
        self.disabled_path = root / "disabled_voices.json"
        self.dashboard = ReadingDashboard(
            ReadingDictionary(str(self.readings_path)),
            self.router,
            sessions=SessionStore(root / "sessions"),
            password_file=str(self.password_path),
        )
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.router.close()
        self.temp_dir.cleanup()

    async def login(self):
        witness = witness_for(self.dashboard)
        await register_witness(self.client, self.dashboard, witness)
        return await login_witness(self.client, self.dashboard, witness)

    async def test_lists_switches_and_checks_backends(self):
        await self.login()

        listed = await (await self.client.get("/api/tts/backends")).json()
        self.assertTrue(listed["router"])
        self.assertEqual(listed["active"], "home")
        self.assertEqual([item["id"] for item in listed["backends"]], ["home", "spare"])
        self.assertEqual(listed["backends"][0]["url"], "http://home")
        self.assertTrue(listed["backends"][0]["active"])

        switched = await (await self.client.post("/api/tts/active", json={"id": "spare"})).json()
        self.assertEqual(switched["active"], "spare")
        self.assertEqual(switched["preferred"], "spare")
        self.assertFalse(switched["backends"][0]["active"])

        checked = await (await self.client.post("/api/tts/check")).json()
        self.assertTrue(all(item["healthy"] for item in checked["backends"]))
        self.assertEqual(checked["active"], "spare")

    async def test_rejects_unknown_backend(self):
        await self.login()

        response = await self.client.post("/api/tts/active", json={"id": "nowhere"})

        self.assertEqual(response.status, 400)
        self.assertIn("ありません", (await response.json())["error"])

    async def test_reports_a_down_backend_and_falls_back(self):
        await self.login()
        self.backends["home"].fail = True

        checked = await (await self.client.post("/api/tts/check")).json()

        self.assertEqual(checked["active"], "spare")
        self.assertFalse(checked["backends"][0]["healthy"])
        self.assertIn("接続できません", checked["backends"][0]["error"])

    async def test_voice_catalog_lists_every_engine_and_toggles_globally(self):
        """話者クローンページ: 全エンジンの話者を出し、無効化は共通ファイルに効く。"""

        await self.login()

        listed = await (await self.client.get("/api/voice-catalog")).json()
        self.assertEqual([entry["id"] for entry in listed["engines"]], ["home", "spare"])
        self.assertEqual(
            [voice["name"] for voice in listed["engines"][1]["voices"]], ["spare"])
        self.assertTrue(listed["engines"][0]["active"])
        self.assertTrue(all(voice["enabled"] for entry in listed["engines"] for voice in entry["voices"]))

        off = await self.client.put("/api/voice-catalog", json={"name": "spare", "enabled": False})
        self.assertEqual(off.status, 200)
        self.assertEqual(json.loads(self.disabled_path.read_text("utf-8")), ["spare"])
        self.assertIn("spare", self.router.disabled_voices)
        # 読み込み済みのバックエンドにも即反映される
        self.assertIn("spare", self.backends["spare"].disabled_voices)

        listed = await (await self.client.get("/api/voice-catalog")).json()
        self.assertFalse(listed["engines"][1]["voices"][0]["enabled"])

        on = await self.client.put("/api/voice-catalog", json={"name": "spare", "enabled": True})
        self.assertEqual(on.status, 200)
        self.assertEqual(json.loads(self.disabled_path.read_text("utf-8")), [])
        self.assertNotIn("spare", self.router.disabled_voices)

    async def test_catalog_rejects_disabling_the_none_voice(self):
        await self.login()

        response = await self.client.put("/api/voice-catalog", json={"name": "none", "enabled": False})

        self.assertEqual(response.status, 400)

    async def test_catalog_lists_a_backend_that_is_down(self):
        await self.login()
        self.backends["home"].fail = True

        listed = await (await self.client.get("/api/voice-catalog")).json()

        self.assertEqual([entry["id"] for entry in listed["engines"]], ["home", "spare"])
        self.assertIn("接続できません", listed["engines"][0]["error"])
        self.assertEqual(listed["engines"][1]["voices"][0]["name"], "spare")

    async def test_panel_page_is_served(self):
        response = await self.client.get("/tts-panel")

        self.assertEqual(response.status, 200)
        self.assertIn("/api/tts/stream", await response.text())

    async def test_stream_mode_and_failover_can_be_changed(self):
        await self.login()
        self.backends["home"].supports_streaming = True
        self.backends["home"].stream = lambda *args, **kwargs: None

        listed = await (await self.client.get("/api/tts/backends")).json()
        self.assertEqual(listed["backends"][0]["stream_mode"], "stream")
        self.assertFalse(listed["backends"][1]["stream_capable"])

        changed = await (await self.client.post("/api/tts/stream", json={"id": "home", "mode": "wav"})).json()
        self.assertEqual(changed["backends"][0]["stream_mode"], "wav")

        refused = await self.client.post("/api/tts/stream", json={"id": "spare", "mode": "stream"})
        self.assertEqual(refused.status, 400)
        bad = await self.client.post("/api/tts/stream", json={"id": "home", "mode": "fast"})
        self.assertEqual(bad.status, 400)

        toggled = await (await self.client.post("/api/tts/failover", json={"enabled": False})).json()
        self.assertFalse(toggled["failover"])
        bad = await self.client.post("/api/tts/failover", json={"enabled": "no"})
        self.assertEqual(bad.status, 400)


if __name__ == "__main__":
    unittest.main()


class DuckCurveApiTests(ReadingDashboardTests):
    """音量カーブの設定（Web UI から編集）。"""

    async def test_requires_login(self):
        self.assertEqual((await self.client.get("/api/duck-curve")).status, 401)
        self.assertEqual(
            (await self.client.post("/api/duck-curve", json={"points": []})).status, 401,
        )

    async def test_get_returns_curve_and_limits(self):
        await self.login()

        data = await (await self.client.get("/api/duck-curve")).json()

        self.assertTrue(data["curve"]["points"])
        self.assertEqual(data["limits"]["max_points"], 8)
        self.assertEqual(data["defaults"]["points"][-1]["volume"], 0.2)

    async def test_post_saves_and_applies_to_players(self):
        from jllm_plugins.ducking.duck_curve import DuckCurve

        class FakeDucker:
            curve = DuckCurve.default()

        class FakePlayer:
            def __init__(self):
                self.ducker = FakeDucker()
                self.applied = []

            def set_curve(self, curve):
                self.applied.append(curve)

        player = FakePlayer()
        self.reader.players = {1: player}
        await self.login()

        response = await self.client.post("/api/duck-curve", json={
            "points": [{"seconds": 0, "volume": 1.0},
                       {"seconds": 2, "volume": 0.4},
                       {"seconds": 6, "volume": 0.15}],
            "attack": 0.4, "release": 0.1, "hold_seconds": 1.2,
        })
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["saved"])
        self.assertEqual(data["applied"], 1)
        self.assertEqual(len(player.applied), 1)
        self.assertEqual([point["volume"] for point in data["curve"]["points"]],
                         [1.0, 0.4, 0.15])
        self.assertAlmostEqual(data["curve"]["reaction_seconds"], 2.0)
        self.assertAlmostEqual(player.applied[0].target(3.0), 0.4)
        self.assertAlmostEqual(player.applied[0].target(7.0), 0.15)

    async def test_post_clamps_and_sorts_points(self):
        await self.login()

        data = await (await self.client.post("/api/duck-curve", json={
            "points": [{"seconds": 5, "volume": 5}, {"seconds": -3, "volume": -1}],
            "attack": 99, "release": 0, "hold_seconds": 99,
        })).json()

        points = data["curve"]["points"]
        self.assertEqual([point["seconds"] for point in points], [0.0, 5.0])
        self.assertEqual([point["volume"] for point in points], [0.0, 1.0])
        self.assertLessEqual(data["curve"]["attack"], 1.0)
        self.assertGreaterEqual(data["curve"]["release"], 0.02)


class ListeningApiTests(ReadingDashboardTests):
    """Web UI の「聞き取り」（空気読みの様子）。"""

    async def test_guests_can_watch_but_speakers_and_curve_are_hidden(self):
        class FakePlayer:
            def listening_status(self):
                return {
                    "guild_id": "1",
                    "duck": {"enabled": True, "volume": 0.4, "level": 0.2,
                             "trigger_seconds": 1.0, "hold_seconds": 0.8,
                             "speakers": ["42"], "curve": {"samples": [1]}},
                }

        self.reader.players = {1: FakePlayer()}
        self.reader.bot = None

        data = await (await self.client.get("/api/listening")).json()

        # 未ログインでも見られる
        self.assertFalse(data["authenticated"])
        self.assertTrue(data["watching"])
        # 喋っている人とカーブは伏せる
        self.assertEqual(data["guilds"][0]["duck"]["speakers"], [])
        self.assertNotIn("curve", data["guilds"][0]["duck"])
        self.assertEqual(data["names"], {})
        # 声コマンドの面はもう返さない
        self.assertNotIn("commands", data["guilds"][0])
        self.assertNotIn("entries", data)
        self.assertNotIn("labels", data)

    async def test_reports_the_ducking_state(self):
        class FakePlayer:
            def listening_status(self):
                return {
                    "guild_id": "1",
                    "duck": {"enabled": True, "speaking": True, "active": True,
                             "volume": 0.4, "level": 0.2, "trigger_seconds": 1.0,
                             "hold_seconds": 0.8, "speakers": ["42"],
                             "curve": {"samples": [1]}},
                }

        self.reader.players = {1: FakePlayer()}
        self.reader.bot = None

        await self.login()
        response = await self.client.get("/api/listening")
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["authenticated"])
        self.assertTrue(data["watching"])
        self.assertEqual(data["guilds"][0]["duck"]["volume"], 0.4)
        self.assertIn("curve", data["guilds"][0]["duck"])

    async def test_speaker_names_are_resolved_for_logged_in_viewers(self):
        class FakePlayer:
            def listening_status(self):
                return {
                    "guild_id": "1",
                    "duck": {"enabled": True, "speakers": ["42"]},
                }

        class FakeMember:
            display_name = "だれか"

        class FakeGuild:
            def get_member(self, user_id):
                return FakeMember() if user_id == 42 else None

        class Bot:
            def get_guild(self, guild_id):
                return FakeGuild()

            def get_user(self, user_id):
                return None

        self.reader.players = {1: FakePlayer()}
        self.reader.bot = Bot()

        await self.login()
        data = await (await self.client.get("/api/listening")).json()

        self.assertEqual(data["names"], {"42": "だれか"})

    async def test_endpoint_works_with_a_real_player(self):
        """実物の SpeechPlayer（空気読みつき）でも 200 で返る。

        以前は listening_status が存在しない属性を見ていて 500 になっていた。
        """

        from jllm_plugins.ducking import SpeechTracker, VolumeDucker
        from jllm_plugins.ducking.duck_curve import DuckCurve, Point
        from jllm_plugins.playback.player import SpeechPlayer

        player = SpeechPlayer.__new__(SpeechPlayer)
        player.guild_id = 1
        tracker = SpeechTracker(0.8)
        player.ducker = VolumeDucker(
            tracker, lambda volume: None, tick_seconds=0.04,
            curve=DuckCurve([Point(0.0, 1.0), Point(1.0, 0.2)]),
        )
        tracker.observe_packet(42)
        self.reader.players = {1: player}

        await self.login()
        response = await self.client.get("/api/listening")
        data = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["watching"])
        self.assertTrue(data["guilds"][0]["duck"]["enabled"])
        self.assertEqual(data["guilds"][0]["duck"]["trigger_seconds"], 1.0)

    async def test_curve_is_only_for_logged_in(self):
        class FakePlayer:
            def listening_status(self):
                return {
                    "guild_id": "1",
                    "duck": {"enabled": True, "volume": 0.4, "curve": {"samples": [1]}},
                }

        self.reader.players = {1: FakePlayer()}

        guest = await (await self.client.get("/api/listening")).json()
        self.assertNotIn("curve", guest["guilds"][0]["duck"])

        await self.login()
        member = await (await self.client.get("/api/listening")).json()
        self.assertIn("curve", member["guilds"][0]["duck"])

    async def test_without_players_it_says_not_watching(self):
        self.reader.players = {}
        await self.login()

        data = await (await self.client.get("/api/listening")).json()

        self.assertFalse(data["watching"])
        self.assertEqual(data["guilds"], [])

    async def test_dashboard_page_has_the_listening_tab(self):
        await self.login()

        html = await (await self.client.get("/")).text()

        self.assertIn('id="listenTab"', html)
        self.assertIn('id="listenPanel"', html)
        self.assertIn('id="listenCurve"', html)
        self.assertIn("/api/listening", html)
        # 未ログインでも開ける（タブは管理者限定にしない）
        self.assertIn('<button id="listenTab" class="tab"', html)
        # 声コマンド（STT）の面は消えている
        self.assertNotIn("声コマンド", html)
        self.assertNotIn("認識した言葉", html)
        self.assertNotIn("拾う言葉", html)
        self.assertNotIn("VOICE_COMMANDS_ENABLED", html)

    async def test_dashboard_page_has_the_overview_tab(self):
        await self.login()

        html = await (await self.client.get("/")).text()

        self.assertIn('id="overviewTab"', html)
        self.assertIn('id="overviewPanel"', html)
        # 未ログインでも開ける（タブは管理者限定にしない）
        self.assertIn('<button id="overviewTab" class="tab"', html)
        # 「音声ファイル」タブの直後、既存タブの並びは変えない
        self.assertLess(html.index('data-tab="sound"'), html.index('id="overviewTab"'))
        self.assertLess(html.index('id="overviewTab"'), html.index('data-tab="reading"'))
        # ?tab=overview で開ける（許可リストに入っている）
        self.assertIn('"overview", "recorder"', html)
        # モックアップの内容がそのまま入っている
        for heading in [
            "概念図",
            "Web UI（ダッシュボード）",
            "コマンド一覧",
            "コマンド以外の操作",
            "読み上げの効果（本文に書くトークン）",
            "掛かる範囲のルール",
            "読み上げを止める・飛ばす",
        ]:
            self.assertIn(heading, html)
        # カスタムCSSは ov- 接頭辞で統合し、トークンは再定義しない
        self.assertIn(".ov-diagram", html)
        css = html.split("全体像（?tab=overview）", 1)[1].split("</style>", 1)[0]
        self.assertIn(".ov-diagram", css)
        self.assertNotIn("--bg:", css)
        self.assertNotIn("--panel:", css)
        self.assertNotIn("color-scheme", css)
