# ruff: noqa: E501, E402
import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from .dashboard_dictionaries import MAX_UPLOAD_BYTES, DictionaryAdmin
from .dashboard_passkeys import PasskeyAdmin
from .dashboard_voice_requests import VoiceRequests
from .dashboard_voices import VoiceAdmin


@dataclass
class DashboardConfig:
    d1_api_url: str = "http://127.0.0.1:8083"
    d1_timeout_seconds: float = 20.0
    duck_curve_path: str = "data/duck_curve.json"
    readings_dashboard_url: str = "http://127.0.0.1:8765"
    talk_model: str = "sarashina3-mini"
    turtle_soup_max_questions: int = 100
    turtle_soup_per_ip: int = 60
    webchat_dir: str = "data/webchat"
    webchat_max_text: int = 2000
    webchat_per_ip: int = 12
from jllm_tts import TTSBackendError

from jllm_plugins.audio_effects import EFFECTS
from jllm_plugins.conversation.conversation import WEBCHAT_SYSTEM
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.ducking.duck_curve import MAX_POINTS, MAX_SECONDS, MAX_SPEED, MIN_SPEED, DuckCurve
from jllm_plugins.llm import prompts
from jllm_plugins.playback.player import load_duck_curve
from jllm_plugins.tts_router.router import TTSRouter

from .playground import MAX_TEXT_LENGTH, Playground, PlaygroundError
from .turtle_soup import (
    MAX_GUESS_CHARS,
    MAX_QUESTION_CHARS,
    D1Client,
    D1Error,
    SoupError,
    TurtleSoup,
)
from .voice_cloning import MAX_VOICE_UPLOAD_BYTES

logger = logging.getLogger(__name__)
SESSION_COOKIE = "reading_dashboard_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


class ReadingDashboard(DictionaryAdmin, VoiceAdmin, PasskeyAdmin, VoiceRequests):
    def __init__(
        self,
        readings: ReadingDictionary,
        tts,
        *,
        reader=None,
        sessions=None,
        webchat=None,
        webchat_client=None,
        soup=None,
        password_file: str = ".dashboard-auth.json",
        host: str = "127.0.0.1",
        port: int = 8765,
        public_url: str | None = None,
        require_existing_passkey: bool = False,
        effects=None,
        config=None,
    ):
        self.config = config or DashboardConfig()
        public_url = public_url or self.config.readings_dashboard_url
        self.readings = readings
        self.tts = tts
        self.reader = reader
        self.sessions = sessions
        self.webchat_client = webchat_client
        self._webchat_hits: dict[str, list[float]] = {}
        self.password_path = Path(password_file)
        self.host = host
        self.port = port
        self._sessions: dict[str, float] = {}
        self._runner: web.AppRunner | None = None
        self._voice_admin_lock = asyncio.Lock()
        self._init_passkeys(public_url)
        self.require_existing_passkey = require_existing_passkey
        if require_existing_passkey and not self._passkeys["credentials"]:
            raise RuntimeError("既存の管理者パスキーが必要です。認証データの保存先を確認してください。")
        # 認証情報を検証してから Web チャットの DB を作る。
        self.webchat = webchat if webchat is not None else SessionStore(self.config.webchat_dir)
        self._init_voice_requests()

        # 実験場。ログイン不要で誰でも試せるので、回数制限をかける。
        dictionary = getattr(reader, "dictionary", None)
        self.playground = (
            Playground(tts, dictionary, readings, effects=effects)
            if dictionary is not None else None
        )
        self._playground_hits: dict[str, list[float]] = {}
        self._playground_total: list[float] = []

        # ウミガメのスープ。d1 を出題者にする（判定だけ GPU を使う）。
        self.soup = soup if soup is not None else TurtleSoup(
            D1Client(self.config.d1_api_url, self.config.d1_timeout_seconds),
            max_questions=self.config.turtle_soup_max_questions,
        )
        self._turtle_hits: dict[str, list[float]] = {}

        self.app = web.Application(
            middlewares=[self._security_middleware],
            client_max_size=max(MAX_UPLOAD_BYTES, MAX_VOICE_UPLOAD_BYTES) + 1024 * 1024,
        )
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/recorder", self._recorder),
                web.get("/tts-panel", self._tts_panel),
                web.get("/favicon.svg", self._favicon),
                web.get("/favicon.ico", self._favicon),
                web.get("/apple-touch-icon.png", self._apple_touch_icon),
                web.get("/api/playground/effects", self._playground_effects),
                web.post("/api/playground", self._playground_speech),
                # ウミガメのスープ。ログイン不要（実験場と同じく回数制限つき）。
                web.get("/api/turtle/puzzles", self._turtle_puzzles),
                web.get("/api/turtle", self._turtle_state),
                web.post("/api/turtle/new", self._turtle_new),
                web.post("/api/turtle/ask", self._turtle_ask),
                web.post("/api/turtle/guess", self._turtle_guess),
                web.post("/api/turtle/hint", self._turtle_hint),
                web.post("/api/turtle/reveal", self._turtle_reveal),
                web.post("/api/voice-requests", self._submit_voice_request),
                web.get("/api/voice-requests", self._protect(self._list_voice_requests)),
                web.get("/api/voice-requests/{request_id}/file", self._protect(self._voice_request_file)),
                web.post("/api/voice-requests/{request_id}/review", self._protect(self._review_voice_request)),
                # 会話セッションは未ログインでも見られる。ただし未ログインでは
                # ユーザー名を匿名にし、ID とアイコンは返さない。
                web.get("/api/webchat/sessions", self._protect(self._list_webchat)),
                web.post("/api/webchat/sessions", self._protect(self._create_webchat)),
                web.get("/api/webchat/sessions/{session_id}", self._protect(self._webchat_detail)),
                web.delete("/api/webchat/sessions/{session_id}", self._protect(self._delete_webchat)),
                web.post("/api/webchat/sessions/{session_id}/stream", self._protect(self._webchat_stream)),
                web.get("/api/sessions", self._list_sessions),
                web.get("/api/sessions/{session_id}", self._session_detail),
                web.get("/api/session", self._session_status),
                web.post("/api/logout", self._logout),
                web.get("/api/passkeys", self._protect(self._list_passkeys)),
                web.delete("/api/passkeys", self._protect(self._delete_passkey)),
                web.post("/api/passkeys/register/options", self._passkey_register_options),
                web.post("/api/passkeys/register/verify", self._passkey_register_verify),
                web.post("/api/passkeys/login/options", self._passkey_login_options),
                web.post("/api/passkeys/login/verify", self._passkey_login_verify),
                web.get("/api/readings", self._get_readings),
                web.post("/api/readings", self._protect(self._add_reading)),
                web.put("/api/readings", self._protect(self._edit_reading)),
                web.delete("/api/readings", self._protect(self._delete_reading)),
                web.get("/api/sounds", self._list_sounds),
                web.put("/api/sounds", self._protect(self._edit_sound)),
                web.delete("/api/sounds", self._protect(self._remove_sound)),
                web.post("/api/sounds/upload", self._protect(self._upload_sound)),
                web.get("/api/sounds/file", self._sound_file),
                web.get("/api/gifs", self._list_gifs),
                web.put("/api/gifs", self._protect(self._edit_gif)),
                web.delete("/api/gifs", self._protect(self._remove_gif)),
                web.post("/api/gifs/upload", self._protect(self._upload_gif)),
                web.get("/api/gifs/file", self._gif_file),
                # 聞き取りは未ログインでも見られる（名前は伏せる。カーブはログイン時のみ）
                web.get("/api/listening", self._listening_status),
                web.get("/api/voice-debug", self._voice_debug),
                web.get("/api/duck-curve", self._protect(self._get_duck_curve)),
                web.post("/api/duck-curve", self._protect(self._set_duck_curve)),
                web.get("/api/tts/backends", self._protect(self._tts_status)),
                web.post("/api/tts/active", self._protect(self._set_tts_active)),
                web.post("/api/tts/check", self._protect(self._check_tts)),
                web.post("/api/tts/stream", self._protect(self._set_tts_stream)),
                web.post("/api/tts/failover", self._protect(self._set_tts_failover)),
                web.get("/api/voices", self._protect(self._get_voices)),
                web.get("/api/voice-library", self._protect(self._voice_library)),
                web.put("/api/voice-library", self._protect(self._set_voice_enabled)),
                web.get("/api/voice-catalog", self._protect(self._voice_catalog)),
                web.put("/api/voice-catalog", self._protect(self._set_catalog_voice)),
                web.put("/api/voice-library/volume", self._protect(self._set_voice_volume)),
                web.put("/api/voice-library/speed", self._protect(self._set_voice_speed)),
                web.post("/api/voice-library/upload", self._protect(self._add_voice)),
                web.delete("/api/voice-library", self._protect(self._delete_voice)),
                web.post("/api/generate", self._protect(self._generate)),
            ]
        )

    # ------------------------------------------------------------ 聞き取り

    def _voice_name(self, guild_id, user_id) -> str | None:
        """そのサーバーで見えている名前を返す。"""

        bot = getattr(self.reader, "bot", None)

        if bot is None or not user_id:
            return None

        try:
            numeric = int(user_id)
        except (TypeError, ValueError):
            return None

        if guild_id:
            try:
                guild = bot.get_guild(int(guild_id))
            except (TypeError, ValueError):
                guild = None

            member = guild.get_member(numeric) if guild is not None else None

            if member is not None:
                return getattr(member, "display_name", None) or getattr(member, "name", None)

        user = bot.get_user(numeric)

        return getattr(user, "display_name", None) or getattr(user, "name", None)

    async def _listening_status(self, request: web.Request) -> web.Response:
        """空気読みのいまの様子（リアルタイム表示用）。

        未ログインでも見られる。ただしカーブ（音量の履歴）と名前は出さない。
        """

        authenticated = self._session_token(request) is not None
        players = getattr(self.reader, "players", None) or {}
        guilds = []
        names: dict[str, str] = {}

        for player in players.values():
            getter = getattr(player, "listening_status", None)

            if getter is None:
                continue

            try:
                status = getter()
            except Exception:  # noqa: BLE001 見取り図なので落とさない
                logger.warning("聞き取りの状態を取れませんでした", exc_info=True)
                continue

            if not authenticated:
                guilds.append(self._public_listening(status))
                continue

            guilds.append(status)

            # 喋っている人を Discord の表示名で出す（IDだけだと読みにくい）
            for user_id in (status.get("duck") or {}).get("speakers") or []:
                name = self._voice_name(status.get("guild_id"), user_id)

                if name:
                    names[str(user_id)] = name

        return web.json_response({
            "guilds": guilds,
            "names": names,
            "watching": bool(guilds),
            "authenticated": authenticated,
        })

    def _players(self) -> list:
        return list((getattr(self.reader, "players", None) or {}).values())

    async def _voice_debug(self, request: web.Request) -> web.Response:
        """音声接続・送受信・DAVE・ミュートの状態（原因切り分け用）。

        読み上げの動作には影響しない。数値と接続状態だけを返す。
        """

        guilds = []

        for player in self._players():
            getter = getattr(player, "voice_status", None)

            if getter is None:
                continue

            try:
                guilds.append(getter())
            except Exception:  # noqa: BLE001 見取り図なので落とさない
                logger.warning("音声接続の状態を取れませんでした", exc_info=True)

        return web.json_response({"guilds": guilds, "watching": bool(guilds)})

    def _current_curve(self) -> DuckCurve:
        """いま使われているカーブ（走っていなければ保存されたもの）。"""

        for player in self._players():
            ducker = getattr(player, "ducker", None)
            curve = getattr(ducker, "curve", None)

            if curve is not None:
                return curve

        return load_duck_curve()

    async def _get_duck_curve(self, request: web.Request) -> web.Response:
        """音量カーブの設定（区間ごとの音量点と速さ）。"""

        curve = self._current_curve()

        return web.json_response({
            "curve": curve.to_dict(),
            "defaults": DuckCurve.default().to_dict(),
            "limits": {
                "max_points": MAX_POINTS,
                "max_seconds": MAX_SECONDS,
                "min_speed": MIN_SPEED,
                "max_speed": MAX_SPEED,
            },
        })

    async def _set_duck_curve(self, request: web.Request) -> web.Response:
        """音量カーブを保存して、走っている再生に即反映する。"""

        data = await self._json_body(request)
        curve = DuckCurve.from_dict(data)
        saved = curve.save(self.config.duck_curve_path) is not None
        applied = 0

        for player in self._players():
            setter = getattr(player, "set_curve", None)

            if setter is None:
                continue

            try:
                setter(curve)
                applied += 1
            except Exception:  # noqa: BLE001
                logger.warning("音量カーブを反映できませんでした", exc_info=True)

        logger.info("音量カーブを更新しました（保存: %s / 反映: %d台）", saved, applied)

        return web.json_response({
            "curve": curve.to_dict(), "saved": saved, "applied": applied,
        })

    @staticmethod
    def _public_listening(status: dict) -> dict:
        """未ログイン向け。喋っている人とカーブは伏せる。"""

        public = dict(status)
        duck = dict(public.get("duck") or {})
        duck.pop("curve", None)
        duck["speakers"] = []
        public["duck"] = duck

        return public

    @staticmethod
    def _json_error(message: str, status: int) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def _json_body(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, web.HTTPException):
            raise web.HTTPBadRequest(text="JSON形式のリクエストが必要です。") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="JSONオブジェクトが必要です。")
        return data

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != request.host.lower():
                    return self._json_error("不正なアクセス元です。", 403)

        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if request.path == "/recorder" else "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _session_token(self, request: web.Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        expires = self._sessions.get(token)
        if expires is None or expires <= time.monotonic():
            self._sessions.pop(token, None)
            return None
        self._sessions[token] = time.monotonic() + SESSION_TTL_SECONDS
        return token

    def _protect(self, handler):
        @wraps(handler)
        async def guarded(request: web.Request):
            if self._session_token(request) is None:
                return self._json_error("ログインしてください。", 401)
            return await handler(request)

        return guarded

    async def _index(self, request: web.Request) -> web.StreamResponse:
        page = Path(__file__).with_name("dashboard.html")
        return web.FileResponse(page, headers={"Cache-Control": "no-store"})

    async def _tts_panel(self, request: web.Request) -> web.StreamResponse:
        """読み上げエンジンと配信方式だけを触る小さな画面（API はログイン必須）。"""

        return web.FileResponse(Path(__file__).with_name("tts_panel.html"),
                                headers={"Cache-Control": "no-store"})

    async def _recorder(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(Path(__file__).with_name("recorder.html"),
                                headers={"Cache-Control": "no-store"})

    # アイコンは services/ に置いた実ファイルを返す（favicon.ico も中身は同じSVG）。
    async def _favicon(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            Path(__file__).with_name("favicon.svg"),
            headers={
                "Content-Type": "image/svg+xml; charset=utf-8",
                "Cache-Control": "public, max-age=86400",
            },
        )

    async def _apple_touch_icon(self, request: web.Request) -> web.StreamResponse:
        path = Path(__file__).with_name("apple-touch-icon.png")

        if not path.is_file():
            return self._json_error("アイコンがありません。", 404)

        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    # -------------------------------------------------------- Webチャット

    def _webchat_allow(self, request: web.Request) -> bool:
        """WebチャットはAPIを消費するので、IPごとに回数を制限する。"""
        now = time.monotonic()
        cutoff = now - 60.0
        peer = getattr(request, "remote", None) or "unknown"
        hits = [value for value in self._webchat_hits.get(peer, []) if value > cutoff]

        if len(hits) >= self.config.webchat_per_ip:
            self._webchat_hits[peer] = hits
            return False

        hits.append(now)
        self._webchat_hits[peer] = hits
        return True

    async def _list_webchat(self, request: web.Request) -> web.Response:
        """Webチャットの会話一覧（管理者のみ）。"""
        rows = await asyncio.to_thread(self.webchat.all_sessions, 200)

        for row in rows:
            row["channel_name"] = "Webチャット"

        return web.json_response({"sessions": rows, "assistant": self._assistant()})

    async def _create_webchat(self, request: web.Request) -> web.Response:
        """新しい会話を作る（管理者のみ）。"""
        session_id = await asyncio.to_thread(
            self.webchat.create_session, "web", "web", "web", "Web",
            self.config.talk_model, WEBCHAT_SYSTEM,
        )
        return web.json_response({"session_id": session_id})

    async def _delete_webchat(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "")
        removed = await asyncio.to_thread(self.webchat.delete_session, session_id)

        if not removed:
            return self._json_error("その会話はありません。", 404)

        return web.json_response({"deleted": session_id})

    async def _webchat_detail(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "")
        data = await asyncio.to_thread(self.webchat.detail, session_id, 200, 20000)

        if not data:
            return self._json_error("その会話はありません。", 404)

        data["session"]["channel_name"] = "Webチャット"
        data["assistant"] = self._assistant()
        return web.json_response(data)

    def _webchat_prompt(self, session_id) -> str:
        settings = self.webchat.settings(session_id)
        return settings.get("system_prompt") or WEBCHAT_SYSTEM

    def _webchat_messages(self, session_id):
        """会話の履歴をモデル用の messages にする。"""
        prompt = self._webchat_prompt(session_id)
        plain = prompts.uses_plain(self.webchat_client)
        messages = [] if plain else [{"role": "system", "content": prompt}]

        for record in self.webchat.history(session_id, limit=60):
            if not record["text"].strip():
                continue

            if record["author"] == "Bot":
                messages.append({"role": "assistant", "content": record["text"]})
            else:
                messages.append({"role": "user", "content": record["text"]})

        if plain and prompt and prompt != WEBCHAT_SYSTEM and messages:
            # 独自のシステムプロンプトだけは捨てずに、最後の発言の先頭へ畳む。
            prompts.set_user(messages, prompt.strip() + "\n\n" + prompts.msg_user(messages))

        return messages

    async def _webchat_stream(self, request: web.Request) -> web.StreamResponse:
        """1往復をSSEで返す（トークンごとに少しずつ）。"""
        session_id = request.match_info.get("session_id", "")

        if await asyncio.to_thread(self.webchat.session, session_id) is None:
            return self._json_error("その会話はありません。", 404)

        try:
            payload = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSONが必要です。", exc.status)

        text = str(payload.get("text") or "").strip()

        if not text:
            return self._json_error("文章を入力してください。", 400)

        if len(text) > self.config.webchat_max_text:
            return self._json_error(f"文章は{self.config.webchat_max_text}文字までです。", 400)

        if not self._webchat_allow(request):
            return self._json_error("少し待ってから送ってください。", 429)

        client = self.webchat_client

        if client is None:
            return self._json_error("会話モデルが設定されていません。", 503)

        settings = await asyncio.to_thread(self.webchat.settings, session_id)
        model = settings.get("model") or self.config.talk_model
        await asyncio.to_thread(self.webchat.append_user, session_id, "web", "Web", text)
        messages = await asyncio.to_thread(self._webchat_messages, session_id)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async def send(event, data):
            await response.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            )

        await send("start", {"session_id": session_id, "model": model})
        chunks, started = [], time.monotonic()

        try:
            async for delta in client.stream(messages, max_tokens=1200, model=model):
                chunks.append(delta)
                await send("delta", {"text": delta})
        except Exception:
            logger.exception("Webチャットの生成に失敗")
            await send("error", {"message": "生成に失敗しました。少し待って試してください。"})
            return response

        answer = "".join(chunks).strip()

        if not answer:
            await send("error", {"message": "空の応答でした。"})
            return response

        await asyncio.to_thread(self.webchat.append_assistant, session_id, answer)
        await send("done", {
            "content": answer,
            "elapsed": round(time.monotonic() - started, 1),
            "model": model,
        })
        return response

    # -------------------------------------------------------- 会話セッション

    @staticmethod
    def _anonymous(label) -> str:
        """未ログイン用の表示名。同じ人は同じ名前になる（IDは返さないので追跡は不可）。"""
        digest = hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:4].upper()
        return "ゲスト" + digest

    def _is_anonymous(self, request: web.Request) -> bool:
        return self._session_token(request) is None

    def _assistant(self) -> dict:
        """会話UIに出すBot自身の表示名とアイコン。"""
        user = getattr(getattr(self.reader, "bot", None), "user", None)

        if user is None:
            return {"name": "jllm-voice", "avatar_url": ""}

        name = getattr(user, "display_name", None) or getattr(user, "name", "") or "jllm-voice"
        avatar = getattr(user, "avatar", None)
        key = getattr(avatar, "key", None)
        url = ""

        if key:
            suffix = "gif" if key.startswith("a_") else "png"
            url = f"https://cdn.discordapp.com/avatars/{user.id}/{key}.{suffix}?size=64"

        return {"name": name, "avatar_url": url}

    def _channel_name(self, channel_id):
        """Discord のチャンネル名（取れなければ ID のまま）。"""
        bot = getattr(self.reader, "bot", None)

        if bot is None:
            return ""

        try:
            channel = bot.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return ""

        return getattr(channel, "name", "") or ""

    async def _list_sessions(self, request: web.Request) -> web.Response:
        """会話セッションの一覧（管理者のみ）。"""
        if self.sessions is None:
            return self._json_error("この環境では会話セッションを扱えません。", 503)

        try:
            limit = max(1, min(1000, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200

        channel = request.query.get("channel") or None

        try:
            rows = await asyncio.to_thread(
                self.sessions.all_sessions, limit, channel, None,
            )
        except Exception:
            logger.exception("会話セッションの一覧を取得できませんでした")
            return self._json_error("会話セッションを読み込めませんでした。", 500)

        anonymous = self._is_anonymous(request)
        users = {} if anonymous else await asyncio.to_thread(
            self.sessions.users_map, [row.get("parent_user") for row in rows],
        )

        for row in rows:
            row["channel_name"] = self._channel_name(row.get("channel_id"))

            if anonymous:
                row["parent_user_name"] = self._anonymous(row.get("parent_user"))
                row["parent_user"] = ""
                row["parent_user_avatar"] = ""
            else:
                profile = users.get(str(row.get("parent_user")), {})
                row["parent_user_avatar"] = profile.get("avatar_url", "")

        total = await asyncio.to_thread(self.sessions.count_sessions, channel, None)
        return web.json_response({
            "sessions": rows, "total": total, "anonymous": anonymous,
            "assistant": self._assistant(),
        })

    async def _session_detail(self, request: web.Request) -> web.Response:
        """1セッションの会話（管理者のみ）。"""
        if self.sessions is None:
            return self._json_error("この環境では会話セッションを扱えません。", 503)

        session_id = request.match_info.get("session_id", "")

        try:
            data = await asyncio.to_thread(self.sessions.detail, session_id)
        except Exception:
            logger.exception("会話セッションを取得できませんでした: %s", session_id)
            return self._json_error("会話セッションを読み込めませんでした。", 500)

        if not data:
            return self._json_error("そのセッションはありません。", 404)

        session = data["session"]
        session["channel_name"] = self._channel_name(session.get("channel_id"))
        anonymous = self._is_anonymous(request)

        if anonymous:
            # 名前・アイコン・ID を伏せる。会話の中身と時刻はそのまま見せる。
            for turn in data.get("turns", []):
                if turn.get("user_id"):
                    turn["user_name"] = self._anonymous(turn["user_id"])

                turn["user_id"] = None

            data["users"] = {}
            session["parent_user_name"] = self._anonymous(session.get("parent_user"))
            session["parent_user"] = ""
        else:
            # 発言のアイコン（Discord CDN）。Botの発言には無いので空のまま。
            data["users"] = await asyncio.to_thread(
                self.sessions.users_map,
                [turn.get("user_id") for turn in data.get("turns", [])]
                + [session.get("parent_user")],
            )

        data["anonymous"] = anonymous
        data["assistant"] = self._assistant()
        return web.json_response(data)

    async def _session_status(self, request: web.Request) -> web.Response:
        authenticated = self._session_token(request) is not None
        return web.json_response(
            {
                "authenticated": authenticated,
                "passkey_registered": bool(self._passkeys["credentials"]),
            }
        )

    @staticmethod
    def _secure_cookie(request):
        return request.secure or (
            request.remote in {"127.0.0.1", "::1"}
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        )

    def _create_session(self, request):
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.monotonic() + SESSION_TTL_SECONDS
        response = web.json_response({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._secure_cookie(request),
            samesite="Strict",
            path="/",
        )
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            self._sessions.pop(token, None)
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def _get_readings(self, request: web.Request) -> web.Response:
        entries = [
            {"trigger": trigger, "reading": reading}
            for trigger, reading in self.readings.items()
        ]
        return web.json_response({"entries": entries, "total": len(entries)})

    async def _add_reading(self, request: web.Request) -> web.Response:
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)
        trigger = data.get("trigger")
        reading = data.get("reading")
        if not isinstance(trigger, str) or not isinstance(reading, str):
            return self._json_error("登録語と読みを入力してください。", 400)
        trigger = trigger.strip()
        reading = reading.strip()
        if not trigger or not reading:
            return self._json_error("登録語と読みを空欄にはできません。", 400)
        if len(trigger) > 100 or len(reading) > 200:
            return self._json_error("登録語は100文字、読みは200文字以内で入力してください。", 400)
        try:
            self.readings.add(trigger, reading)
        except OSError:
            logger.exception("読み上げ辞書の保存に失敗しました")
            return self._json_error("辞書ファイルに保存できませんでした。", 500)
        return web.json_response(
            {"ok": True, "trigger": trigger, "reading": reading},
            status=201,
        )

    async def _delete_reading(self, request: web.Request) -> web.Response:
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)
        trigger = data.get("trigger")
        if not isinstance(trigger, str) or not trigger.strip():
            return self._json_error("削除する登録語を指定してください。", 400)
        try:
            removed = self.readings.remove(trigger)
        except OSError:
            logger.exception("読み上げ辞書の削除保存に失敗しました")
            return self._json_error("辞書ファイルに保存できませんでした。", 500)
        if not removed:
            return self._json_error("登録語が見つかりません。", 404)
        return web.json_response({"ok": True})

    # ------------------------------------------------- 読み上げエンジン（TTS）

    def _tts_router(self):
        """多重化しているときだけ TTSRouter を返す。"""

        return self.tts if isinstance(self.tts, TTSRouter) else None

    def _tts_describe(self) -> str:
        """TTS の説明文（describe を持たない実装でも落ちないように）。"""

        describe = getattr(self.tts, "describe", None)

        if callable(describe):
            return str(describe())

        return str(getattr(self.tts, "name", "") or "")

    def _tts_payload(self, status: dict) -> dict:
        return {
            "router": True,
            "describe": self._tts_describe(),
            **status,
        }

    async def _tts_status(self, request: web.Request) -> web.Response:
        router = self._tts_router()

        if router is None:
            return web.json_response({
                "router": False,
                "describe": self._tts_describe() if self.tts is not None else "",
                "backends": [],
            })

        return web.json_response(self._tts_payload(router.status()))

    async def _set_tts_active(self, request: web.Request) -> web.Response:
        router = self._tts_router()

        if router is None:
            return self._json_error("この構成では読み上げエンジンを切り替えられません。", 400)

        try:
            payload = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSONが必要です。", exc.status)

        try:
            result = await router.set_active(
                str(payload.get("id") or ""),
                preferred=payload.get("preferred", True) is not False,
            )
        except TTSBackendError as exc:
            return self._json_error(str(exc), 400)

        logger.info(
            "読み上げエンジンを %s に切り替えました（疎通 %s / ライフサイクル %s）",
            result["active"], "OK" if result["healthy"] else "NG",
            "あり" if result.get("lifecycle") else "なし",
        )
        return web.json_response(self._tts_payload(router.status()))

    async def _check_tts(self, request: web.Request) -> web.Response:
        router = self._tts_router()

        if router is None:
            return web.json_response({
                "router": False,
                "describe": self._tts_describe() if self.tts is not None else "",
                "backends": [],
            })

        return web.json_response(self._tts_payload(await router.check_all()))

    async def _set_tts_stream(self, request: web.Request) -> web.Response:
        """バックエンドごとの配信方式（``stream`` = 届いた順に鳴らす / ``wav`` = 全部待つ）。"""

        router = self._tts_router()

        if router is None:
            return self._json_error("この構成では配信方式を切り替えられません。", 400)

        try:
            payload = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSONが必要です。", exc.status)

        mode = str(payload.get("mode") or "")

        if mode not in {"stream", "wav"}:
            return self._json_error("mode は stream か wav です。", 400)

        try:
            router.set_streaming(str(payload.get("id") or ""), mode == "stream")
        except TTSBackendError as exc:
            return self._json_error(str(exc), 400)

        logger.info("読み上げの配信方式を %s = %s にしました", payload.get("id"), mode)
        return web.json_response(self._tts_payload(router.status()))

    async def _set_tts_failover(self, request: web.Request) -> web.Response:
        router = self._tts_router()

        if router is None:
            return self._json_error("この構成では自動退避を切り替えられません。", 400)

        try:
            payload = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSONが必要です。", exc.status)

        enabled = payload.get("enabled")

        if not isinstance(enabled, bool):
            return self._json_error("enabled は true / false です。", 400)

        router.set_failover(enabled)
        logger.info("読み上げの自動退避を %s にしました", "入" if enabled else "切")
        return web.json_response(self._tts_payload(router.status()))

    async def _get_voices(self, request: web.Request) -> web.Response:
        try:
            speakers = await self.tts.speakers()
        except Exception:
            logger.exception("TTS話者一覧の取得に失敗しました")
            return self._json_error("TTS話者一覧を取得できません。接続設定を確認してください。", 502)
        return web.json_response({"speakers": speakers})

    async def _generate(self, request: web.Request) -> web.Response:
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)

        text = data.get("text")
        speaker = data.get("voice_name", data.get("speaker", 1))
        raw_speed = data.get("speed", 1.0)
        if not isinstance(text, str) or not text.strip():
            return self._json_error("生成する文章を入力してください。", 400)
        if len(text) > 1000:
            return self._json_error("文章は1000文字以内にしてください。", 400)
        if isinstance(speaker, bool) or not isinstance(speaker, (str, int)):
            return self._json_error("話者を選択してください。", 400)
        if isinstance(speaker, str):
            speaker = speaker.strip()
            if not speaker or len(speaker) > 128:
                return self._json_error("話者の指定が不正です。", 400)
            if speaker.isdecimal() and "voice_name" not in data:
                speaker = int(speaker)
        if isinstance(speaker, str) and speaker in getattr(self.tts, 'disabled_voices', set()):
            return self._json_error('この話者は無効です。有効化してから試聴してください。', 400)
        if isinstance(raw_speed, bool):
            return self._json_error("速度は0.5〜2.0で指定してください。", 400)
        try:
            speed = float(raw_speed)
        except (TypeError, ValueError):
            return self._json_error("速度は0.5〜2.0で指定してください。", 400)
        if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            return self._json_error("速度は0.5〜2.0で指定してください。", 400)

        processed = self.readings.apply(text.strip())
        try:
            audio = await self.tts.synthesize(processed, speaker, speed)
        except Exception:
            logger.exception("ダッシュボードからの音声生成に失敗しました")
            return self._json_error("音声生成に失敗しました。TTSサーバーの状態を確認してください。", 502)
        return web.Response(
            body=audio,
            content_type="audio/wav",
            headers={"X-Processed-Text": quote(processed, safe="")},
        )

    # ------------------------------------------------------------ 実験場
    PLAYGROUND_PER_IP = 8
    PLAYGROUND_GLOBAL = 40
    PLAYGROUND_WINDOW = 60.0

    def _playground_allow(self, request: web.Request) -> bool:
        """ログイン不要の入口なので、IP ごとと全体で回数を制限する。"""
        now = time.monotonic()
        cutoff = now - self.PLAYGROUND_WINDOW

        self._playground_total = [t for t in self._playground_total if t > cutoff]
        if len(self._playground_total) >= self.PLAYGROUND_GLOBAL:
            return False

        peer = request.remote or "unknown"
        hits = [t for t in self._playground_hits.get(peer, []) if t > cutoff]
        if len(hits) >= self.PLAYGROUND_PER_IP:
            self._playground_hits[peer] = hits
            return False

        hits.append(now)
        self._playground_hits[peer] = hits
        self._playground_total.append(now)
        return True

    async def _playground_effects(self, request: web.Request) -> web.Response:
        """実験場で選べる効果と話者の一覧。"""
        if self.playground is None:
            return self._json_error("この環境では実験場を使えません。", 503)

        voices: list[str] = []
        try:
            listed = await self.tts.list_voices()
        except Exception:
            logger.warning("実験場の話者一覧を取得できませんでした")
            listed = []

        disabled = getattr(self.tts, "disabled_voices", None) or set()
        for voice in listed:
            name = getattr(voice, "name", None)
            if name is None:
                name = str(voice)
            if name and name not in disabled:
                voices.append(str(name))

        return web.json_response({
            "effects": [
                {"code": code, "label": label, "description": description}
                for code, (label, _builder, description) in EFFECTS.items()
            ],
            "voices": voices,
            "max_text": MAX_TEXT_LENGTH,
        })

    async def _playground_speech(self, request: web.Request) -> web.Response:
        if self.playground is None:
            return self._json_error("この環境では実験場を使えません。", 503)

        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)

        text = data.get("text")
        speaker = data.get("voice_name", data.get("speaker"))
        raw_speed = data.get("speed", 1.0)

        if not isinstance(text, str) or not text.strip():
            return self._json_error("文章を入力してください。", 400)
        if len(text) > MAX_TEXT_LENGTH:
            return self._json_error(
                f"文章は{MAX_TEXT_LENGTH}文字以内にしてください。", 400
            )
        if not isinstance(speaker, (str, int)) or isinstance(speaker, bool):
            return self._json_error("話者を選択してください。", 400)
        if isinstance(speaker, str):
            speaker = speaker.strip()
            if not speaker or len(speaker) > 128:
                return self._json_error("話者の指定が不正です。", 400)

        try:
            speed = float(raw_speed)
        except (TypeError, ValueError):
            return self._json_error("速度は0.5〜2.0で指定してください。", 400)
        if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            return self._json_error("速度は0.5〜2.0で指定してください。", 400)

        if not self._playground_allow(request):
            return self._json_error(
                "実験場の利用が混み合っています。少し待ってから試してください。", 429
            )

        try:
            audio = await self.playground.synthesize(text.strip(), speaker, speed)
        except PlaygroundError as exc:
            return self._json_error(str(exc), 400)
        except Exception:
            logger.exception("実験場の音声生成に失敗しました")
            return self._json_error(
                "音声生成に失敗しました。TTSサーバーの状態を確認してください。", 502
            )

        return web.Response(body=audio, content_type="audio/wav")

    # ------------------------------------------------------ ウミガメのスープ

    def _turtle_allow(self, request: web.Request) -> bool:
        """判定は GPU を使うので、IPごとに回数を制限する。"""

        now = time.monotonic()
        cutoff = now - 60.0
        peer = request.remote or "unknown"
        hits = [t for t in self._turtle_hits.get(peer, []) if t > cutoff]

        if len(hits) >= self.config.turtle_soup_per_ip:
            self._turtle_hits[peer] = hits
            return False

        hits.append(now)
        self._turtle_hits[peer] = hits
        return True

    @staticmethod
    def _turtle_payload(game) -> dict:
        return {
            "game": game.public(),
            "limits": {
                "max_question_chars": MAX_QUESTION_CHARS,
                "max_guess_chars": MAX_GUESS_CHARS,
                "max_questions": game.max_questions,
            },
        }

    async def _turtle_puzzles(self, request: web.Request) -> web.Response:
        """同梱の問題一覧（問題文と答えは含めない）。"""

        return web.json_response({"puzzles": self.soup.puzzle_list()})

    async def _turtle_state(self, request: web.Request) -> web.Response:
        game = self.soup.get(request.query.get("game"))

        if game is None:
            return self._json_error("ゲームが見つかりません。新しく始めてください。", 404)

        return web.json_response(self._turtle_payload(game))

    async def _turtle_new(self, request: web.Request) -> web.Response:
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)

        try:
            game = self.soup.new_game(data.get("puzzle"))
        except SoupError as exc:
            return self._json_error(str(exc), 400)

        return web.json_response(self._turtle_payload(game), status=201)

    async def _turtle_turn(self, request: web.Request, kind: str) -> web.Response:
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)

        try:
            game = self.soup.require(data.get("game"))
        except SoupError as exc:
            return self._json_error(str(exc), 404)

        if kind in ("question", "guess"):
            text = data.get("text")

            if not isinstance(text, str) or not text.strip():
                return self._json_error(
                    "質問を入力してください。" if kind == "question" else "推測を入力してください。",
                    400,
                )

            if not self._turtle_allow(request):
                return self._json_error(
                    "判定が混み合っています。少し待ってから試してください。", 429
                )

        try:
            if kind == "question":
                await game.ask(self.soup.client, data.get("text"))
            elif kind == "guess":
                await game.guess(self.soup.client, data.get("text"))
            elif kind == "hint":
                game.hint()
            else:
                game.reveal()
        except SoupError as exc:
            return self._json_error(str(exc), 400)
        except D1Error as exc:
            logger.warning("ウミガメのスープの判定に失敗しました: %s", exc)
            return self._json_error(str(exc), 502)

        return web.json_response(self._turtle_payload(game))

    async def _turtle_ask(self, request: web.Request) -> web.Response:
        return await self._turtle_turn(request, "question")

    async def _turtle_guess(self, request: web.Request) -> web.Response:
        return await self._turtle_turn(request, "guess")

    async def _turtle_hint(self, request: web.Request) -> web.Response:
        return await self._turtle_turn(request, "hint")

    async def _turtle_reveal(self, request: web.Request) -> web.Response:
        return await self._turtle_turn(request, "reveal")

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=logger)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("読み上げ辞書ダッシュボード: http://%s:%s/", self.host, self.port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        await self.soup.close()
