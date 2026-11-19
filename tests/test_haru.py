# ruff: noqa: E501
import asyncio
import io
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import numpy as np
import sphn
from aiohttp import FormData, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.dashboard.dashboard import DashboardConfig, ReadingDashboard
from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.ducking import VOICE_RECV_AVAILABLE, attach_pcm_tap, build_ducker, detach_pcm_tap
from jllm_plugins.haru.bridge import (
    HARU_FRAME_SAMPLES,
    HARU_SAMPLE_RATE,
    KIND_AUDIO,
    KIND_HANDSHAKE,
    KIND_TEXT,
    HaruAudioSource,
    HaruConversationSession,
    HaruInputMixer,
)
from jllm_plugins.haru.client import HaruApiClient, HaruConfig
from jllm_plugins.haru.cog import HaruCog
from jllm_plugins.haru.control import (
    HaruControlError,
    HaruController,
    NoHaruController,
    build_haru_controller,
)
from jllm_plugins.haru.personas import HaruPersonaStore, build_system_prompt, normalize_haru_voice
from jllm_plugins.playback.mixer import FRAME_BYTES, GuildAudioMixer

from tests.passkey_auth import login_witness, register_witness, witness_for


def _make_wav_bytes(seconds: float, rate: int = 24000, amp: int = 8000) -> bytes:
    buf = io.BytesIO()
    total = int(seconds * rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(total):
            sample = int(amp * math.sin(2 * math.pi * 220 * i / rate))
            frames.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class HaruPersonaTests(unittest.TestCase):
    def test_persona_store_crud_and_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HaruPersonaStore(root / "personas.json", root / "voices", root / "focus.json")
            saved = store.save_persona(
                name="ハル",
                persona="明るいアシスタント。",
                gender="female",
                backchannel="high",
                relationship="友人。",
                situation="雑談。",
                guidance_scale=1.7,
                voice_wav=b"RIFFfake",
                voice_seconds=6.5,
            )
            self.assertEqual(saved["name"], "ハル")
            self.assertEqual(saved["guidance_scale"], 1.7)
            self.assertIsNotNone(store.voice_path(saved))

            prompt = build_system_prompt(saved, speaker_name="りゅう")
            self.assertIn("【あなたの名前】\nハル", prompt)
            self.assertIn("【あなたの性別】\n女性", prompt)
            self.assertIn("【あなたの相づち】\n多め", prompt)
            self.assertIn("【相手の名前】\nりゅう", prompt)

            updated = store.save_persona(
                persona_id=saved["id"],
                name="ハル改",
                custom_prompt="カスタムプロンプト",
            )
            self.assertEqual(build_system_prompt(updated, speaker_name="ignored"), "カスタムプロンプト")

            store.set_focus(123, 456)
            reloaded = HaruPersonaStore(root / "personas.json", root / "voices", root / "focus.json")
            self.assertEqual(reloaded.get("ハル改")["id"], saved["id"])
            self.assertEqual(reloaded.get_focus(123), 456)
            self.assertTrue(reloaded.delete_persona(saved["id"]))
            self.assertEqual(reloaded.list_personas(), [])

    def test_normalize_haru_voice_validates_duration(self):
        short = _make_wav_bytes(1.0)
        with self.assertRaises(ValueError):
            normalize_haru_voice(short, ".wav")
        ok_wav = _make_wav_bytes(3.5)
        normalized, sec = normalize_haru_voice(ok_wav, ".wav")
        self.assertGreater(len(normalized), 1000)
        self.assertGreaterEqual(sec, 3.0)


class HaruAudioMixerTests(unittest.IsolatedAsyncioTestCase):
    def test_input_mixer_focus_and_resampling(self):
        mixer = HaruInputMixer(focus_user_id=None)
        # 48kHz stereo 20ms (960 frames) for two users
        u1 = np.full((960, 2), 4000, dtype="<i2").tobytes()
        u2 = np.full((960, 2), 2000, dtype="<i2").tobytes()
        mixer.push_discord_pcm(1, u1)
        mixer.push_discord_pcm(2, u2)
        frame = mixer.pop_frame(480)
        self.assertEqual(frame.shape, (480,))
        self.assertAlmostEqual(float(frame[0]), 6000 / 32768.0, places=4)

        mixer.set_focus(2)
        mixer.push_discord_pcm(1, u1)
        mixer.push_discord_pcm(2, u2)
        focused = mixer.pop_frame(480)
        self.assertAlmostEqual(float(focused[0]), 2000 / 32768.0, places=4)

    def test_guild_audio_mixer_combines_haru_and_reader_overlay(self):
        class ConstantSource(discord.AudioSource):
            def __init__(self, sample_val: int, reads: int = 1):
                self.val = sample_val
                self.reads = reads
                self.cleaned = False

            def read(self):
                if self.reads <= 0:
                    return b""
                self.reads -= 1
                return np.full(1920, self.val, dtype="<i2").tobytes()

            def is_opus(self):
                return False

            def cleanup(self):
                self.cleaned = True

        haru_src = HaruAudioSource(volume=1.0)
        haru_src.push_24k_float(np.full(480, 0.25, dtype=np.float32))
        mixer = GuildAudioMixer(haru_src)
        done = []
        overlay = ConstantSource(1000, reads=1)
        mixer.play_overlay(overlay, after=lambda err: done.append(err))

        mixed = mixer.read()
        self.assertEqual(len(mixed), FRAME_BYTES)
        samples = np.frombuffer(mixed, dtype="<i2")
        self.assertGreater(int(samples[10]), 5000)

        # Second read triggers EOF + after callback on overlay while primary continues
        mixer.read()
        self.assertTrue(overlay.cleaned)
        self.assertEqual(done, [None])
        self.assertFalse(mixer.has_overlay())
        mixer.close()

    @unittest.skipUnless(VOICE_RECV_AVAILABLE, "voice_recv が無い環境")
    async def test_listener_hub_coexists_between_ducker_and_pcm_tap(self):
        from discord.ext import voice_recv

        client = voice_recv.VoiceRecvClient.__new__(voice_recv.VoiceRecvClient)
        sinks = []
        client.is_listening = Mock(side_effect=lambda: bool(sinks))
        client.stop_listening = Mock(side_effect=lambda: sinks.clear())
        client.listen = Mock(side_effect=lambda sink: sinks.append(sink))

        ducker = build_ducker(client, Mock(), level=0.3, hold_seconds=0.2)
        try:
            self.assertTrue(sinks[-1].wants_opus())
            tapped = []
            with patch("jllm_plugins.ducking.install_dave_support") as inst:
                self.assertTrue(attach_pcm_tap(client, "haru", lambda uid, pcm: tapped.append((uid, pcm))))
                inst.assert_called_once()
            self.assertFalse(sinks[-1].wants_opus())

            user = SimpleNamespace(id=77, bot=False)
            sinks[-1].write(user, SimpleNamespace(pcm=b"\x01\x02\x03\x04"))
            self.assertEqual(tapped, [(77, b"\x01\x02\x03\x04")])
            self.assertGreater(ducker.tracker.packets, 0)

            detach_pcm_tap(client, "haru")
            self.assertTrue(sinks[-1].wants_opus())
        finally:
            await ducker.stop()


class HaruClientAndSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_and_ws_conversation_roundtrip(self):
        ref_calls = 0
        sessions_created = []
        received_audio_pages = []

        async def handle_ready(_req):
            return web.json_response({"ready": True})

        async def handle_ref(req):
            nonlocal ref_calls
            ref_calls += 1
            body = await req.read()
            self.assertGreater(len(body), 0)
            return web.json_response({"id": f"ref-{ref_calls}", "seconds": 4.0, "trimmed": False})

        async def handle_session(req):
            data = await req.json()
            if data["reference_audio_id"] == "ref-1" and len(sessions_created) == 0:
                sessions_created.append("expired-once")
                return web.json_response({"code": "reference_not_found", "error": "missing"}, status=404)
            sessions_created.append(data)
            return web.json_response({"id": "sess-1"})

        async def handle_ws(req):
            ws = web.WebSocketResponse()
            await ws.prepare(req)
            await ws.send_bytes(bytes([KIND_HANDSHAKE]))
            writer = sphn.OpusStreamWriter(HARU_SAMPLE_RATE)
            pcm = np.full(HARU_FRAME_SAMPLES, 0.1, dtype=np.float32)
            pages = writer.append_pcm(pcm)
            if pages:
                await ws.send_bytes(bytes([KIND_AUDIO]) + pages)
            await ws.send_bytes(bytes([KIND_TEXT]) + "こんにちは。".encode())

            async for msg in ws:
                if msg.type == WSMsgType.BINARY and msg.data and msg.data[0] == KIND_AUDIO:
                    received_audio_pages.append(msg.data[1:])
                    if len(received_audio_pages) >= 1:
                        break
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/api/ready", handle_ready)
        app.router.add_post("/api/reference_audio", handle_ref)
        app.router.add_post("/api/session", handle_session)
        app.router.add_get("/api/chat", handle_ws)

        server = TestServer(app)
        await server.start_server()
        client = HaruApiClient(str(server.make_url("/")).rstrip("/"))
        try:
            self.assertTrue(await client.ready())
            sid = await client.create_session(
                persona_id="haru",
                system_prompt="テストプロンプト",
                audio=b"wavdata",
                guidance_scale=1.5,
            )
            self.assertEqual(sid, "sess-1")
            self.assertEqual(ref_calls, 2)

            ws = await client.connect_chat(sid)
            transcripts = []
            fake_vc = SimpleNamespace(
                is_playing=lambda: False,
                play=Mock(),
                stop=Mock(),
            )

            async def on_text(_cid, txt):
                transcripts.append(txt)

            with (
                patch("jllm_plugins.haru.bridge.attach_pcm_tap", return_value=True),
                patch("jllm_plugins.haru.bridge.detach_pcm_tap"),
            ):
                session = HaruConversationSession(
                    guild_id=10,
                    voice_channel_id=20,
                    text_channel_id=30,
                    persona={"id": "haru", "name": "ハル"},
                    voice_client=fake_vc,
                    ws=ws,
                    on_transcript=on_text,
                )
                session.start()
                for _ in range(40):
                    if transcripts and received_audio_pages:
                        break
                    await asyncio.sleep(0.05)
                await session.close()

            self.assertEqual(transcripts, ["こんにちは。"])
            self.assertGreaterEqual(len(received_audio_pages), 1)
        finally:
            await client.close()
            await server.close()


class HaruCogAndDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_commands_and_dashboard_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HaruPersonaStore(root / "personas.json", root / "voices", root / "focus.json")
            api_client = HaruApiClient("http://127.0.0.1:8998")
            api_client.ready = AsyncMock(return_value=True)
            bot = SimpleNamespace(
                get_guild=lambda gid: SimpleNamespace(
                    id=gid,
                    name="TestGuild",
                    voice_client=None,
                    get_member=lambda uid: SimpleNamespace(id=uid, display_name="太郎"),
                    get_channel=lambda cid: None,
                ),
                get_channel=lambda cid: None,
                guilds=[],
            )
            cog = HaruCog(bot, store=store, client=api_client, config=HaruConfig())

            # Dashboard setup
            readings = ReadingDictionary(str(root / "readings.json"))
            reader = SimpleNamespace(
                dictionary=SoundDictionary(str(root / "sounds.json"), str(root / "sounds")),
                media_dir=root / "media",
                media={},
                bot=bot,
                players={},
            )
            reader.media_dir.mkdir()
            dashboard = ReadingDashboard(
                readings,
                SimpleNamespace(describe=lambda: "fake"),
                reader=reader,
                sessions=SessionStore(str(root / "sessions")),
                webchat=SessionStore(str(root / "webchat")),
                haru=cog,
                password_file=str(root / ".dashboard-auth.json"),
                config=DashboardConfig(duck_curve_path=str(root / "duck.json")),
            )
            witness = witness_for(dashboard)
            client = TestClient(TestServer(dashboard.app))
            await client.start_server()
            try:
                # Unauthorized guard
                unauth = await client.get("/api/haru/state")
                self.assertEqual(unauth.status, 401)

                await register_witness(client, dashboard, witness)
                await login_witness(client, dashboard, witness)

                # Create persona via multipart upload
                form = FormData()
                form.add_field("name", "ハル")
                form.add_field("gender", "female")
                form.add_field("backchannel", "mid")
                form.add_field("persona", "優しいアシスタント。")
                form.add_field("guidance_scale", "1.6")
                form.add_field(
                    "voice",
                    _make_wav_bytes(3.5),
                    filename="sample.wav",
                    content_type="audio/wav",
                )
                created = await client.post("/api/haru/personas", data=form)
                self.assertEqual(created.status, 201, await created.text())
                payload = await created.json()
                pid = payload["persona"]["id"]
                self.assertTrue(payload["persona"]["has_voice"])

                # Voice preview endpoint
                voice_resp = await client.get(f"/api/haru/personas/{pid}/voice")
                self.assertEqual(voice_resp.status, 200)
                self.assertGreater(len(await voice_resp.read()), 1000)

                # Autocomplete returns created persona
                choices = await cog._persona_autocomplete(SimpleNamespace(), "ハ")
                self.assertEqual(len(choices), 1)
                self.assertEqual(choices[0].value, pid)

                # Set focus via WebUI & /haru focus slash command
                focus_resp = await client.post(
                    "/api/haru/focus", json={"guild_id": "100", "user_id": "555"}
                )
                self.assertEqual(focus_resp.status, 200)
                self.assertEqual(store.get_focus(100), 555)

                inter = SimpleNamespace(
                    guild=SimpleNamespace(id=100),
                    user=SimpleNamespace(id=1),
                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                await cog.haru_focus.callback(cog, inter, None)
                self.assertIsNone(store.get_focus(100))

                # Delete persona via WebUI
                deleted = await client.delete(f"/api/haru/personas/{pid}")
                self.assertEqual(deleted.status, 200)
                self.assertEqual(store.list_personas(), [])

                # Haru server start/stop + VRAM free/restore endpoints
                no_ctrl = build_haru_controller({})
                self.assertIsInstance(no_ctrl, NoHaruController)
                with self.assertRaises(HaruControlError):
                    await no_ctrl.start()

                fake_ctrl = SimpleNamespace(
                    configured=True,
                    start=AsyncMock(
                        return_value={
                            "ok": True,
                            "active": True,
                            "healthy": True,
                            "vram": {"used_mib": 9300, "free_mib": 2982, "total_mib": 12282},
                        }
                    ),
                    stop=AsyncMock(
                        return_value={
                            "ok": True,
                            "active": False,
                            "healthy": False,
                            "vram": {"used_mib": 400, "free_mib": 11882, "total_mib": 12282},
                        }
                    ),
                    status=AsyncMock(
                        return_value={
                            "ok": True,
                            "active": True,
                            "healthy": True,
                            "vram": {"used_mib": 9300, "free_mib": 2982, "total_mib": 12282},
                        }
                    ),
                )
                cog.controller = fake_ctrl
                dashboard.vram = SimpleNamespace(
                    configured=True,
                    status=AsyncMock(
                        return_value={"ok": True, "vram": {"used_mib": 400, "free_mib": 11882, "total_mib": 12282}}
                    ),
                    free=AsyncMock(
                        return_value={"ok": True, "vram": {"used_mib": 300, "free_mib": 11982, "total_mib": 12282}}
                    ),
                    restore=AsyncMock(
                        return_value={"ok": True, "vram": {"used_mib": 4500, "free_mib": 7782, "total_mib": 12282}}
                    ),
                )
                start_resp = await client.post("/api/haru/server/start", json={})
                self.assertEqual(start_resp.status, 200)
                self.assertTrue((await start_resp.json())["state"]["server_active"])

                stop_resp = await client.post("/api/haru/server/stop", json={})
                self.assertEqual(stop_resp.status, 200)
                fake_ctrl.stop.assert_awaited_once()

                free_resp = await client.post("/api/vram/free", json={})
                self.assertEqual(free_resp.status, 200)
                self.assertIn("11982MiB", (await free_resp.json())["vram_text"])

                restore_resp = await client.post("/api/vram/restore", json={})
                self.assertEqual(restore_resp.status, 200)
                self.assertIn("4500MiB", (await restore_resp.json())["vram_text"])

                real_ctrl = HaruController("ryu@192.168.0.22", "/tmp/k", "/tmp/kh")
                with self.assertRaises(HaruControlError):
                    await real_ctrl.run("invalid")
            finally:
                await client.close()
                await api_client.close()


if __name__ == "__main__":
    unittest.main()
