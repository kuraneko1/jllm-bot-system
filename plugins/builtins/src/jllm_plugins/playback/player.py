import array
import asyncio
import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass

import discord
from jllm_tts import TTSBackend, TTSBackendError, TTSContentError

from jllm_plugins.audio_effects import build_filter, describe_effects
from jllm_plugins.ducking.duck_curve import DuckCurve


@dataclass(frozen=True)
class PlayerConfig:
    speaker_id: int = 1
    speed: float = 1.2
    volume: float = 0.2
    sample_volume: float = 0.2
    allow_nonverbal_emojis: bool = False
    max_queue_size: int = 30
    tail_silence_ms: int = 200
    ir_path: str = "data/ir/room.wav"
    duck_enabled: bool = False
    duck_curve_path: str = "data/duck_curve.json"
    duck_level: float = 0.2
    duck_hold_seconds: float = 0.8
    duck_tick_seconds: float = 0.04
    duck_trigger_seconds: float = 1.0
    duck_debug: bool = False


logger = logging.getLogger(__name__)


def _config_of(player) -> PlayerConfig:
    return getattr(player, "config", None) or PlayerConfig()


PREFETCH_SIZE = 3

#: ストリーミング再生する生 PCM の形式（drama3-stream の仕様）。
STREAM_RATE = 44100
STREAM_CHANNELS = 1
STREAM_WIDTH = 2

#: ストリーミング再生の上限（秒）。生成が止まったまま返ってこない場合の保険。
STREAM_PLAY_TIMEOUT = 300.0

#: 音声接続を見張る間隔と、切れてから入り直すまでの猶予（秒）
VOICE_WATCHDOG_INTERVAL = 5.0
VOICE_RECONNECT_GRACE = 15.0


def load_duck_curve(config: PlayerConfig | None = None) -> DuckCurve:
    """保存された音量カーブを読む（無ければ設定から作る）。"""
    config = config or PlayerConfig()

    return DuckCurve.load(
        config.duck_curve_path,
        fallback=DuckCurve.from_config(
            trigger_seconds=config.duck_trigger_seconds,
            level=config.duck_level,
            attack=0.25,
            release=0.06,
            hold_seconds=config.duck_hold_seconds,
        ),
    )


# Irodori-TTS-Server の speed 受付範囲（Field ge=0.25, le=4.0）。
# 範囲外はサーバー側で弾かれるため、はみ出し分は手元の atempo で補う。
TTS_SERVER_MIN_SPEED = 0.25
TTS_SERVER_MAX_SPEED = 4.0


@dataclass(frozen=True)
class SpeechItem:
    text: str
    speaker_id: int | str
    speed: float
    sound_path: str | None = None
    volume: float = 1.0
    effects: tuple = ()
    post_id: str | None = None
    author_key: str | None = None


@dataclass(frozen=True)
class AudioChunk:
    post_id: str | None
    author_key: str | None
    audio: bytes | None
    #: ストリーミング再生するもの（``audio`` は使わない）。
    stream: StreamingAudio | None = None


@dataclass
class StreamingAudio:
    """生成しながら鳴らす生 PCM（s16le モノラル）。

    最初のブロック（``head``）だけ受け取った状態で持つ。残りは
    ``chunks`` から届いた順に読む。**生成と再生を別タスクに分ける**ための入れ物。
    """

    item: SpeechItem
    chunks: object
    head: bytes
    #: 生PCMのサンプルレート。バックエンドごとに違う（DRAMA3 は 44.1kHz、
    #: Irodori は 48kHz）ので、鳴らす側はここを見る。
    rate: int = STREAM_RATE
    #: 鳴らしている途中で「この音声は使えない」と分かった（先頭が無音など）。
    #: 再生を止めて wav の経路で作り直す。
    unusable: bool = False

    async def aclose(self) -> None:
        close = getattr(self.chunks, "aclose", None)

        if callable(close):
            with contextlib.suppress(Exception):
                await close()


def install_send_stats(voice_client) -> dict:
    """送信パケットの数を数える（「本当に送っているか」を見るため）。

    ``_get_voice_packet`` は RTP ヘッダを付ける直前に呼ばれるので、ここを通った数が
    そのまま送信パケット数になる。計測できない接続では空の dict を返す。
    """

    stats = getattr(voice_client, "_jllm_send_stats", None)

    if stats is not None:
        return stats

    original = getattr(voice_client, "_get_voice_packet", None)

    if not callable(original):
        return {}

    stats = {"packets": 0, "bytes": 0, "encrypted": 0, "last": 0.0}

    def _get_voice_packet(data):
        packet = original(data)
        stats["packets"] += 1
        stats["bytes"] += len(packet)
        stats["last"] = time.monotonic()

        connection = getattr(voice_client, "_connection", None)
        encrypted = bool(getattr(connection, "can_encrypt", False))

        if encrypted:
            stats["encrypted"] += 1
        elif getattr(connection, "dave_protocol_version", 0) and not stats.get("warned"):
            # E2EE の部屋なのに暗号化できない＝Discord 側で捨てられて無音になる
            stats["warned"] = True
            logger.warning(
                "音声を暗号化できません（DAVE session 未確立: protocol=%s）。"
                "この状態では Discord 側で無音になることがあります",
                getattr(connection, "dave_protocol_version", None),
            )

        if not stats.get("started"):
            stats["started"] = True
            logger.info(
                "音声パケットの送信を開始しました（DAVE=%s / 暗号化=%s）",
                getattr(connection, "dave_protocol_version", 0),
                encrypted,
            )

        return packet

    voice_client._get_voice_packet = _get_voice_packet
    voice_client._jllm_send_stats = stats

    return stats


class SpeechPlayer:
    def __init__(
        self,
        voice_client: discord.VoiceClient,
        tts: TTSBackend,
        text_channel_id: int,
        config: PlayerConfig | None = None,
        effects=None,
    ):
        self.voice_client = voice_client
        self.tts = tts
        self.text_channel_id = text_channel_id
        self.guild_id = getattr(getattr(voice_client, "guild", None), "id", None)

        self.config = config or PlayerConfig()
        self.effects = effects
        self.speaker_id = _config_of(self).speaker_id
        self.speed = _config_of(self).speed
        self.volume = _config_of(self).volume
        self.sample_volume = _config_of(self).sample_volume

        self.allow_nonverbal_emojis = _config_of(self).allow_nonverbal_emojis
        self.skip_users: set[int] = set()

        # 空気読み（人が喋っている間だけ音量を下げる）※実験機能
        self.ducker = None
        self.current_source: discord.PCMVolumeTransformer | None = None
        self.duck_volume = 1.0
        #: ストリーミング再生で ffmpeg 側にかける音量（wav は焼き込み済みなので 1.0）。
        self.current_gain = 1.0

        self.queue: asyncio.Queue[SpeechItem] = asyncio.Queue(
            maxsize=_config_of(self).max_queue_size
        )

        self.audio_queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=PREFETCH_SIZE)

        self.current: AudioChunk | None = None
        self.pending: AudioChunk | None = None
        self.dropped_posts: dict[str, None] = {}

        self.closed = False
        self.generation = 0

        # 音声を送れているかを数える（原因切り分け用。動作には影響しない）
        self.send_stats = install_send_stats(voice_client)
        self.channel_id = getattr(getattr(voice_client, "channel", None), "id", None)

        self.synth_worker = asyncio.create_task(self._synthesize_loop())
        self.play_worker = asyncio.create_task(self._play_loop())
        # 音声接続が切れたままにならないように見張る（Discord 側の切断は
        # discord.py が自動で張り直すが、失敗すると誰も気づかないまま無音になる）
        self.watchdog = asyncio.create_task(self._watch_voice())

    async def _watch_voice(self):
        """音声接続が切れたままなら入り直す。"""

        down_since = 0.0

        while not self.closed:
            await asyncio.sleep(VOICE_WATCHDOG_INTERVAL)

            try:
                if self.closed:
                    return

                if self.voice_client.is_connected():
                    down_since = 0.0
                    continue

                now = time.monotonic()

                if down_since <= 0.0:
                    down_since = now
                    logger.warning(
                        "音声接続が切れています（%.0f秒たっても戻らなければ入り直します）",
                        VOICE_RECONNECT_GRACE,
                    )
                    continue

                if now - down_since < VOICE_RECONNECT_GRACE:
                    continue

                if await self.reconnect_voice():
                    down_since = 0.0
                else:
                    down_since = now

            except asyncio.CancelledError:
                raise

            except Exception:  # noqa: BLE001 見張りで読み上げを落とさない
                logger.exception("音声接続の見張りでエラー")

    async def reconnect_voice(self) -> bool:
        """いまのチャンネルへ入り直して、空気読みもやり直す。"""

        old = self.voice_client
        channel = getattr(old, "channel", None)

        if channel is None:
            guild = getattr(old, "guild", None)
            channel = (
                guild.get_channel(self.channel_id)
                if guild is not None and self.channel_id is not None
                else None
            )

        if channel is None:
            logger.warning("入り直すボイスチャンネルが分かりません")
            return False

        from jllm_plugins import ducking

        client_class = ducking.voice_recv.VoiceRecvClient if ducking.VOICE_RECV_AVAILABLE else None

        try:
            await asyncio.wait_for(old.disconnect(force=True), timeout=15)
        except Exception:  # noqa: BLE001 既に切れているときは失敗してよい
            logger.debug("古い音声接続の切断に失敗", exc_info=True)

        try:
            new_client = await channel.connect(cls=client_class)
        except Exception:  # noqa: BLE001 次の見回しで再挑戦する
            logger.exception("音声接続の入り直しに失敗しました")
            return False

        self.voice_client = new_client
        self.channel_id = getattr(getattr(new_client, "channel", None), "id", self.channel_id)
        self.send_stats = install_send_stats(new_client)

        if self.ducker is not None:
            await self.ducker.stop()
            self.ducker = None

        await self.enable_listening()
        logger.info("音声接続を入り直しました")

        return True

    def _stop_playback(self) -> None:
        """再生だけ止める。

        ``VoiceRecvClient.stop()`` は受信（空気読み）まで止めてしまうので、
        再生だけを止められる ``stop_playing()`` を使う。無い接続では従来どおり
        ``stop()`` に落とす。
        """

        stop_playing = getattr(self.voice_client, "stop_playing", None)

        if callable(stop_playing):
            stop_playing()
            return

        self.voice_client.stop()

    def skip(self) -> bool:
        if self.closed:
            return False

        stopped = self.voice_client.is_playing() or self.voice_client.is_paused()

        self.generation += 1

        if stopped:
            self._stop_playback()

        had_pending = self._drain(self.queue)
        had_pending = self._drain(self.audio_queue) or had_pending

        return stopped or had_pending

    def skip_current(self) -> bool:
        """現在再生中の音声だけ止め、キューの後続は止めない。"""
        if self.closed:
            return False

        playing = self.voice_client.is_playing() or self.voice_client.is_paused()

        if playing:
            self._stop_playback()

        return playing

    def _mark_dropped(self, post_id: str) -> None:
        self.dropped_posts[post_id] = None

        while len(self.dropped_posts) > 200:
            self.dropped_posts.pop(next(iter(self.dropped_posts)))

    def _is_dropped(self, chunk: AudioChunk) -> bool:
        return chunk.post_id is not None and chunk.post_id in self.dropped_posts

    def _queued_head_author(self) -> str | None:
        if self.pending is not None and self.pending.author_key is not None:
            return self.pending.author_key

        for source in (self.audio_queue._queue, self.queue._queue):
            for item in list(source):
                key = getattr(item, "author_key", None)

                if key is not None:
                    return key

        return None

    def skip_current_post(self) -> bool:
        """再生中の投稿と同作者の待機分を飛ばし、次の投稿へ進める。"""
        if self.closed:
            return False

        target = self.current.author_key if self.current is not None else None

        if target is None:
            target = self._queued_head_author()

        playing = self.voice_client.is_playing() or self.voice_client.is_paused()

        if playing:
            self._stop_playback()

        dropped_any = False

        if self.current is not None and self.current.post_id is not None:
            self._mark_dropped(self.current.post_id)
            dropped_any = True

        if target is not None:
            if (
                self.pending is not None
                and self.pending.author_key == target
                and self.pending.post_id is not None
            ):
                self._mark_dropped(self.pending.post_id)
                dropped_any = True

            for source in (self.audio_queue._queue, self.queue._queue):
                for item in list(source):
                    if (
                        getattr(item, "author_key", None) == target
                        and getattr(item, "post_id", None) is not None
                    ):
                        self._mark_dropped(item.post_id)
                        dropped_any = True

        return playing or dropped_any

    @staticmethod
    def _drain(queue: asyncio.Queue) -> bool:
        drained = False

        while True:
            try:
                queue.get_nowait()

            except asyncio.QueueEmpty:
                return drained

            else:
                drained = True
                queue.task_done()

    def enqueue(
        self,
        text: str,
        speaker: int | str | None = None,
        speed: float | None = None,
        post_id: str | None = None,
        author_key: str | None = None,
        volume: float | None = None,
        effects: tuple | None = None,
    ) -> bool:
        if self.closed:
            return False

        item = SpeechItem(
            text=text,
            speaker_id=(self.speaker_id if speaker is None else speaker),
            speed=self.speed if speed is None else float(speed),
            volume=1.0 if volume is None else float(volume),
            effects=() if effects is None else tuple(effects),
            post_id=post_id,
            author_key=author_key,
        )

        try:
            self.queue.put_nowait(item)
            return True

        except asyncio.QueueFull:
            logger.warning("読み上げキューが満杯。メッセージを破棄")
            return False

    def enqueue_sound(
        self,
        path: str,
        speed: float = 1.0,
        volume: float = 1.0,
        post_id: str | None = None,
        author_key: str | None = None,
        effects: tuple | None = None,
    ) -> bool:
        if self.closed:
            return False

        item = SpeechItem(
            text="",
            speaker_id=self.speaker_id,
            speed=float(speed),
            sound_path=path,
            volume=float(volume),
            effects=() if effects is None else tuple(effects),
            post_id=post_id,
            author_key=author_key,
        )

        try:
            self.queue.put_nowait(item)
            return True

        except asyncio.QueueFull:
            logger.warning("読み上げキューが満杯。サンプルを破棄")
            return False

    @staticmethod
    def _read_sound(path: str) -> bytes | None:
        try:
            with open(path, "rb") as handle:
                return handle.read()

        except OSError:
            logger.exception(
                "サンプル音声の読み込みに失敗: %s",
                path,
            )
            return None

    @staticmethod
    def _atempo_filters(speed: float) -> str:
        filters = []
        value = speed

        while value > 2.0:
            filters.append("atempo=2.0")
            value /= 2.0

        while value < 0.5:
            filters.append("atempo=0.5")
            value /= 0.5

        filters.append(f"atempo={value:.6f}")
        return ",".join(filters)

    @staticmethod
    def _ffmpeg_to_wav(input_path: str, filtrate: str) -> bytes | None:
        handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        output_path = handle.name
        handle.close()

        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    input_path,
                    "-filter:a",
                    filtrate,
                    "-y",
                    output_path,
                ],
                capture_output=True,
            )

            if result.returncode != 0:
                return None

            with open(output_path, "rb") as handle:
                return handle.read()

        except OSError:
            logger.exception("ffmpeg処理に失敗: %s", input_path)
            return None

        finally:
            try:
                os.unlink(output_path)

            except OSError:
                pass

    @staticmethod
    def _apply_speed(path: str, speed: float) -> bytes | None:
        return SpeechPlayer._ffmpeg_to_wav(path, SpeechPlayer._atempo_filters(speed))

    @staticmethod
    def _apply_speed_bytes(audio: bytes, speed: float) -> bytes | None:
        """合成済み音声に速度をかける。失敗時は None。"""
        handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        input_path = handle.name
        handle.close()

        try:
            with open(input_path, "wb") as handle:
                handle.write(audio)

            return SpeechPlayer._ffmpeg_to_wav(input_path, SpeechPlayer._atempo_filters(speed))

        except OSError:
            logger.exception("倍速処理に失敗")
            return None

        finally:
            try:
                os.unlink(input_path)

            except OSError:
                pass

    @staticmethod
    def _scale_wav(audio: bytes, volume: float) -> bytes:
        if volume == 1.0:
            return audio

        try:
            with wave.open(io.BytesIO(audio), "rb") as reader:
                params = reader.getparams()
                frames = reader.readframes(reader.getnframes())

        except wave.Error, EOFError:
            return audio

        if params.sampwidth != 2:
            return audio

        samples = array.array("h")
        samples.frombytes(frames)

        if sys.byteorder != "little":
            samples.byteswap()

        for index, value in enumerate(samples):
            scaled = int(value * volume)

            if scaled > 32767:
                scaled = 32767
            elif scaled < -32768:
                scaled = -32768

            samples[index] = scaled

        if sys.byteorder != "little":
            samples.byteswap()

        output = io.BytesIO()

        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(samples.tobytes())

        return output.getvalue()

    @staticmethod
    def _apply_effects(audio: bytes, effects, config=None, effect_service=None) -> bytes:
        """ffmpegで音声効果をかける。失敗時は元の音声を返す。"""
        config = config or PlayerConfig()
        if effect_service is not None:
            filtrate = effect_service.build_filter(effects)
        else:
            filtrate = build_filter(effects, ir_path=config.ir_path)

        if not filtrate:
            return audio

        input_handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        input_path = input_handle.name
        input_handle.close()

        output_handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        output_path = output_handle.name
        output_handle.close()

        try:
            with open(input_path, "wb") as handle:
                handle.write(audio)

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    input_path,
                    "-filter:a",
                    filtrate,
                    "-y",
                    output_path,
                ],
                capture_output=True,
            )

            if result.returncode != 0:
                logger.warning(
                    "音声効果の適用に失敗（%s）。効果なしで再生",
                    effect_service.describe_effects(effects)
                    if effect_service
                    else describe_effects(effects),
                )
                return audio

            with open(output_path, "rb") as handle:
                return handle.read()

        except OSError:
            logger.exception("音声効果の処理に失敗")
            return audio

        finally:
            for temporary in (input_path, output_path):
                try:
                    os.unlink(temporary)

                except OSError:
                    pass

    def _active(self) -> bool:
        return not self.closed and self.voice_client.is_connected()

    # ------------------------------------------------------------ ストリーミング

    def _can_stream(self, item: SpeechItem) -> bool:
        """この読み上げを、届いた順に鳴らせるか。

        速度（atempo）と効果音は**全体を見ないとかけられない**ので、必要なときは
        従来どおり wav を作ってから鳴らす。音量は ffmpeg 側（``current_gain``）で
        かけられるが、``PCMVolumeTransformer`` が 2.0 で頭打ちなのでそれを超えるなら
        wav 経路に任せる。
        """

        if item.sound_path is not None:
            return False

        if item.effects:
            return False

        # バックエンドが話者ごとの速度を無視する（DRAMA3 の ignore_speaker_speed 等）なら、
        # 実際に読む速度で判断する。サーバの受付範囲外は手元で atempo をかけるので wav へ。
        speed = item.speed
        resolver = getattr(self.tts, "resolve_speed", None)
        server_side = False

        if callable(resolver) and TTS_SERVER_MIN_SPEED <= speed <= TTS_SERVER_MAX_SPEED:
            speed = resolver(speed)
            server_side = True

        if abs(speed - 1.0) > 1e-3:
            # 速度をサーバに任せられるバックエンド（Irodori 等）は、等速でなくても
            # 全体を待たずに鳴らせる。手元で atempo を掛ける必要があるときだけ wav へ。
            if not (server_side and getattr(self.tts, "streams_any_speed", False)):
                return False

        if not getattr(self.tts, "supports_streaming", False):
            return False

        if not hasattr(self.tts, "stream"):
            return False

        gain = self.volume * item.volume

        return 1e-3 < gain <= 2.0

    async def _open_stream(
        self,
        item: SpeechItem,
    ) -> StreamingAudio | None:
        """最初のブロックだけ受け取った状態にする（無理なら ``None``）。

        ここで失敗したものは wav の経路（seed の再試行・atempo・効果音つき）へ戻す。
        ストリーミングは**速さのための近道**であって、唯一の道にはしない。
        """

        chunks = self.tts.stream(
            text=item.text,
            speaker_id=item.speaker_id,
            speed=item.speed,
        )

        try:
            head = await chunks.__anext__()

        except StopAsyncIteration:
            logger.warning("ストリーミングで音声が 1 つも届きませんでした")
            await self._close_stream(chunks)
            return None

        except TTSBackendError as exc:
            logger.warning("ストリーミングを諦めます（%s）", exc)
            await self._close_stream(chunks)
            return None

        except asyncio.CancelledError, Exception:
            await self._close_stream(chunks)
            raise

        if not head:
            await self._close_stream(chunks)
            return None

        return StreamingAudio(
            item,
            chunks,
            head,
            rate=int(getattr(self.tts, "stream_rate", STREAM_RATE) or STREAM_RATE),
        )

    @staticmethod
    async def _close_stream(chunks) -> None:
        close = getattr(chunks, "aclose", None)

        if callable(close):
            with contextlib.suppress(Exception):
                await close()

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        """パイプへ書き切る（途中で短く書かれた場合にも対応）。"""

        view = memoryview(data)

        while view:
            written = os.write(fd, view)

            if written <= 0:
                raise BrokenPipeError("ffmpeg への書き込みが止まりました")

            view = view[written:]

    async def _pump_stream(self, stream: StreamingAudio, write_fd: int) -> None:
        """受け取った PCM を、届いた順に ffmpeg のパイプへ流す。"""

        loop = asyncio.get_running_loop()

        try:
            if stream.head:
                await loop.run_in_executor(None, self._write_all, write_fd, stream.head)

            async for chunk in stream.chunks:
                if chunk:
                    await loop.run_in_executor(None, self._write_all, write_fd, chunk)

        except asyncio.CancelledError:
            raise

        except TTSContentError as exc:
            # 鳴らしながら判定した結果、使えない音声だった（先頭が無音など）。
            # 無音なので止めても聞こえ方は変わらない。wav の経路で作り直す。
            logger.warning("ストリーミングを止めて作り直します: %s", exc)
            stream.unusable = True
            self._stop_playback()

        except TTSBackendError as exc:
            # 途中で切れた（生成が失敗した）。鳴っている分は最後まで鳴らす。
            logger.warning("ストリーミングの受信が途切れました: %s", exc)

        except BrokenPipeError, OSError:
            # 再生を止めた（ffmpeg が先に居なくなった）。想定内。
            pass

        except Exception:
            logger.exception("ストリーミングの供給に失敗")

        finally:
            # EOF を送って、残りを鳴らし切ってから ffmpeg を終わらせる。
            with contextlib.suppress(OSError):
                os.close(write_fd)

    async def _play_stream(self, stream: StreamingAudio) -> bool:
        """届いた順に鳴らす。鳴らし始められなければ ``False``（wav 経路へ）。"""

        loop = asyncio.get_running_loop()
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)

        before = f"-f s16le -ar {stream.rate} -ac {STREAM_CHANNELS}"
        gain = self.volume * stream.item.volume

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                reader,
                pipe=True,
                before_options=before,
            ),
            # 音量は wav に焼き込む代わりにここでかける（空気読みと同じ場所）。
            volume=min(2.0, max(0.0, self.duck_volume * gain)),
        )
        self.current_source = source
        self.current_gain = gain

        pump = asyncio.create_task(self._pump_stream(stream, write_fd))
        finished = loop.create_future()
        started = False

        def on_finished(error: Exception | None):
            if finished.done():
                return

            if error is not None:
                finished.set_exception(error)
            else:
                finished.set_result(None)

        def after(error: Exception | None):
            loop.call_soon_threadsafe(on_finished, error)

        try:
            self.voice_client.play(source, after=after)
            started = True

            await asyncio.wait_for(finished, timeout=STREAM_PLAY_TIMEOUT)

        except TimeoutError:
            logger.warning("ストリーミング再生が %s 秒で終わりませんでした", STREAM_PLAY_TIMEOUT)

            if started:
                self._stop_playback()

        except asyncio.CancelledError:
            if started:
                self._stop_playback()

            raise

        finally:
            pump.cancel()

            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump

            with contextlib.suppress(OSError):
                reader.close()

            await stream.aclose()

            self.current_gain = 1.0

            if not started:
                source.cleanup()

        return not stream.unusable

    async def _synthesize_tts(self, item: SpeechItem) -> bytes | None:
        """wav を 1 本作る（速度・効果音・音量までかけた状態で返す）。"""

        # サーバーの受付範囲外は弾かれて欠けるため、
        # 範囲内だけサーバーに投げ、はみ出し分は手元でかける。
        server_speed = min(
            TTS_SERVER_MAX_SPEED,
            max(TTS_SERVER_MIN_SPEED, item.speed),
        )
        audio = await self.tts.synthesize(
            text=item.text,
            speaker_id=item.speaker_id,
            speed=server_speed,
        )

        if item.speed != server_speed:
            audio = await asyncio.to_thread(
                self._apply_speed_bytes,
                audio,
                item.speed / server_speed,
            )

            if audio is None:
                return None

        if item.effects:
            audio = await asyncio.to_thread(
                self._apply_effects,
                audio,
                item.effects,
                _config_of(self),
                getattr(self, "effects", None),
            )

        return await asyncio.to_thread(
            self._scale_wav,
            audio,
            self.volume * item.volume,
        )

    async def _play_tts(self, item: SpeechItem) -> None:
        """wav を作ってから鳴らす（ストリーミングが使えなかったとき）。"""

        audio = await self._synthesize_tts(item)

        if audio is None or not self._active():
            return

        await self._play(self._pad_wav(audio, _config_of(self)))

    async def _synthesize_loop(self):
        while True:
            item = await self.queue.get()
            generation = self.generation

            try:
                if not self._active():
                    return

                if item.sound_path is not None:
                    audio = await asyncio.to_thread(
                        self._read_sound,
                        item.sound_path,
                    )

                    if audio is None:
                        continue

                    if item.speed != 1.0:
                        audio = await asyncio.to_thread(
                            self._apply_speed,
                            item.sound_path,
                            item.speed,
                        )

                        if audio is None:
                            continue

                    if item.effects:
                        audio = await asyncio.to_thread(
                            self._apply_effects,
                            audio,
                            item.effects,
                            _config_of(self),
                            getattr(self, "effects", None),
                        )

                    audio = await asyncio.to_thread(
                        self._scale_wav,
                        audio,
                        item.volume * self.sample_volume,
                    )

                else:
                    # 届いた順に鳴らせるなら、最初のブロックだけ待って先に流す
                    # （最後のブロックまで待つと初音が 10 秒近く遅れる）。
                    streamed = None

                    if self._can_stream(item):
                        streamed = await self._open_stream(item)

                    if streamed is not None:
                        if not self._active():
                            await streamed.aclose()
                            return

                        if generation != self.generation or (
                            item.post_id is not None and item.post_id in self.dropped_posts
                        ):
                            await streamed.aclose()
                            continue

                        await self.audio_queue.put(
                            AudioChunk(
                                item.post_id,
                                item.author_key,
                                None,
                                streamed,
                            )
                        )
                        continue

                    audio = await self._synthesize_tts(item)

                    if audio is None:
                        continue

                if generation != self.generation:
                    continue

                if item.post_id is not None and item.post_id in self.dropped_posts:
                    continue

                if not self._active():
                    return

                await self.audio_queue.put(
                    AudioChunk(
                        item.post_id,
                        item.author_key,
                        audio,
                    )
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception("メッセージの音声生成に失敗")

            finally:
                self.queue.task_done()

    async def _play_streamed_chunk(self, chunk: AudioChunk) -> None:
        """ストリーミングの 1 件を鳴らし切る（鳴らせなければ wav で作り直す）。"""

        item = chunk.stream.item

        try:
            if not self._active():
                return

            self.current = chunk

            if await self._play_stream(chunk.stream):
                return

            await self._play_tts(item)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("ストリーミング再生に失敗")

        finally:
            self.current = None
            self.audio_queue.task_done()

    async def _play_loop(self):
        while True:
            if self.pending is not None:
                first = self.pending
                self.pending = None
            else:
                first = await self.audio_queue.get()

            if self._is_dropped(first):
                self.audio_queue.task_done()
                continue

            if first.stream is not None:
                # ストリーミングは届いた順に鳴らすので、同じ投稿でも束ねない。
                await self._play_streamed_chunk(first)
                continue

            batch = [first]

            while True:
                try:
                    candidate = self.audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if self._is_dropped(candidate):
                    self.audio_queue.task_done()
                    continue

                if candidate.post_id == first.post_id:
                    batch.append(candidate)
                else:
                    self.pending = candidate
                    break

            try:
                if not self._active():
                    return

                self.current = first

                audios = [chunk.audio for chunk in batch if chunk.audio is not None]

                if audios:
                    combined = self._combine_wavs(audios)

                    if combined is not None:
                        await self._play(self._pad_wav(combined, _config_of(self)))
                    else:
                        for chunk in audios:
                            if not self._active():
                                return

                            await self._play(self._pad_wav(chunk, _config_of(self)))

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception("メッセージの再生に失敗")

            finally:
                self.current = None

                for _ in batch:
                    self.audio_queue.task_done()

    @staticmethod
    def _combine_wavs(
        chunks: list[bytes],
    ) -> bytes | None:
        if not chunks:
            return None

        if len(chunks) == 1:
            return chunks[0]

        params: set[tuple[int, int, int]] = set()
        frames = []

        try:
            for chunk in chunks:
                with wave.open(io.BytesIO(chunk), "rb") as reader:
                    params.add(
                        (
                            reader.getnchannels(),
                            reader.getsampwidth(),
                            reader.getframerate(),
                        )
                    )
                    frames.append(reader.readframes(reader.getnframes()))

        except wave.Error, EOFError:
            return None

        if len(params) != 1:
            return None

        channels, sampwidth, framerate = params.pop()

        output = io.BytesIO()

        with wave.open(output, "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(sampwidth)
            writer.setframerate(framerate)
            writer.writeframes(b"".join(frames))

        return output.getvalue()

    @staticmethod
    def _pad_wav(audio: bytes, config=None) -> bytes:
        config = config or PlayerConfig()
        if config.tail_silence_ms <= 0:
            return audio

        try:
            with wave.open(io.BytesIO(audio), "rb") as reader:
                params = reader.getparams()
                frames = reader.readframes(reader.getnframes())

        except wave.Error, EOFError:
            return audio

        samples = int(params.framerate * config.tail_silence_ms / 1000)
        silence = b"\x00" * (samples * params.nchannels * params.sampwidth)

        output = io.BytesIO()

        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(frames + silence)

        return output.getvalue()

    async def _play(self, audio: bytes):
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        handle = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        )
        path = handle.name

        try:
            handle.write(audio)
        finally:
            handle.close()

        # 音量を再生中にも動かせるようにしておく（空気読みの ducking 用）。
        # 下げている最中に次の音声が始まっても、そのまま下がった音量で始める
        # （音量の状態はプレイヤーで1つ持つ＝wavもTTSも共通）。
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(path),
            volume=self.duck_volume,
        )
        self.current_source = source

        started = False

        def on_finished(error: Exception | None):
            if finished.done():
                return

            if error is not None:
                finished.set_exception(error)
            else:
                finished.set_result(None)

        def after(error: Exception | None):
            loop.call_soon_threadsafe(
                on_finished,
                error,
            )

        try:
            self.voice_client.play(
                source,
                after=after,
            )

            started = True

            await asyncio.wait_for(
                finished,
                timeout=120,
            )

        except TimeoutError, asyncio.CancelledError:
            if started:
                self._stop_playback()

            raise

        finally:
            if not started:
                source.cleanup()

            try:
                os.unlink(path)

            except OSError:
                pass

    def set_curve(self, curve) -> None:
        """音量カーブを差し替える（Web UI からの編集を即反映）。"""

        if self.ducker is not None:
            self.ducker.set_curve(curve)

    def set_duck(self, volume: float) -> None:
        """いま鳴っている音の音量を変える（1.0 で元通り）。

        wav は音量を焼き込んであり、ストリーミングは ffmpeg 側でかけている。
        どちらでも「空気読みの音量 × その音のもともとの音量」になるようにする。
        """

        self.duck_volume = min(1.0, max(0.0, float(volume)))
        source = self.current_source

        if source is not None:
            source.volume = self.duck_volume * getattr(self, "current_gain", 1.0)

    def log_voice_state(self) -> None:
        """音声接続の状態を1行で残す（DAVE とミュートの切り分け用）。"""

        try:
            status = self.voice_status()
        except Exception:  # noqa: BLE001 見取り図なので落とさない
            logger.debug("音声接続の状態を取れませんでした", exc_info=True)
            return

        dave = status.get("dave") or {}
        logger.info(
            "音声接続: 接続=%s / dave protocol=%s / 暗号化可=%s / session ready=%s / "
            "server mute=%s（暗号化できないと Discord 側で無音になることがあります）",
            status.get("connected"),
            dave.get("protocol_version"),
            dave.get("can_encrypt"),
            dave.get("session_ready"),
            status.get("server_mute"),
        )

    async def enable_listening(self):
        """喋っている人の声を聞き取る（空気読み）※実験機能。

        音声は **デコードしない**（パケットが届いている秒数だけで喋りを判定する）。
        """

        if self.closed:
            return

        self.log_voice_state()

        if self.ducker is not None:
            return

        if not _config_of(self).duck_enabled:
            return

        from jllm_plugins import ducking

        self.ducker = ducking.build_ducker(
            self.voice_client,
            self.set_duck,
            level=_config_of(self).duck_level,
            hold_seconds=_config_of(self).duck_hold_seconds,
            tick_seconds=_config_of(self).duck_tick_seconds,
            trigger_seconds=_config_of(self).duck_trigger_seconds,
            debug=_config_of(self).duck_debug,
            curve=load_duck_curve(_config_of(self)),
        )

        if self.ducker is None:
            return

        logger.info(
            "空気読みを有効にしました（喋っている間 %d%% まで下げます）",
            round(_config_of(self).duck_level * 100),
        )

    def listening_status(self) -> dict:
        """いまの聞き取りの状態（Web UI が見る）。"""

        guild_id = getattr(self, "guild_id", None)
        status: dict = {"guild_id": None if guild_id is None else str(guild_id)}

        ducker = self.ducker

        if ducker is None:
            status["duck"] = {"enabled": False}
        else:
            tracker = ducker.tracker
            curve = ducker.curve
            status["duck"] = {
                "enabled": True,
                "speaking": bool(tracker.speaking),
                "active": tracker.active(),
                "volume": round(ducker.volume, 3),
                "level": ducker.level,
                # 反応するまで＝最初に音量が変わる点（カーブが決める）
                "trigger_seconds": curve.reaction_seconds,
                "hold_seconds": curve.hold_seconds,
                "speaking_seconds": round(tracker.speaking_seconds(), 2),
                "speakers": [str(user_id) for user_id in tracker.active_speakers()],
                # 立ち上がり・立ち下がりのカーブ（ログイン時だけ Web UI に出す）
                "curve": ducker.snapshot(),
            }

        return status

    def voice_status(self) -> dict:
        """音声接続と送受信のいまの状態（原因切り分け用）。

        送信パケット数・DAVE の状態・ミュートの状態をまとめて返す。読み上げの
        動作には影響しない。
        """

        voice_client = self.voice_client
        connection = getattr(voice_client, "_connection", None)
        session = getattr(connection, "dave_session", None)
        guild = getattr(voice_client, "guild", None)
        me = getattr(guild, "me", None)
        voice = getattr(me, "voice", None)
        channel = getattr(voice_client, "channel", None)

        ducker = self.ducker
        tracker = getattr(ducker, "tracker", None)

        stats = dict(getattr(self, "send_stats", None) or {})
        last_sent = stats.pop("last", 0.0)
        now = time.monotonic()

        try:
            listening = bool(voice_client.is_listening())
        except Exception:  # noqa: BLE001 接続の種類によっては無い
            listening = False

        return {
            "guild_id": None if self.guild_id is None else str(self.guild_id),
            "connected": bool(voice_client.is_connected()),
            "channel_id": None if channel is None else str(channel.id),
            "playing": bool(voice_client.is_playing()),
            "listening": listening,
            "self_mute": getattr(voice, "self_mute", None),
            "self_deaf": getattr(voice, "self_deaf", None),
            "server_mute": getattr(voice, "mute", None),
            "server_deaf": getattr(voice, "deaf", None),
            "dave": {
                "protocol_version": getattr(connection, "dave_protocol_version", None),
                "can_encrypt": bool(getattr(connection, "can_encrypt", False)),
                "session_ready": None if session is None else bool(session.ready),
                "downgraded": bool(getattr(connection, "dave_downgraded", False)),
            },
            "send": {
                "packets": stats.get("packets"),
                "bytes": stats.get("bytes"),
                "encrypted": stats.get("encrypted"),
                "seconds_since_last": (None if last_sent <= 0.0 else round(now - last_sent, 2)),
            },
            "receive": {
                "packets": None if tracker is None else tracker.packets,
                "seconds_since_last": (
                    None
                    if tracker is None
                    else round(max(0.0, now - tracker._last_packet), 2)
                    if tracker._last_packet > 0.0
                    else None
                ),
            },
        }

    async def close(self):
        if self.closed:
            return

        self.closed = True

        if self.ducker is not None:
            await self.ducker.stop()
            self.ducker = None

        self.synth_worker.cancel()
        self.play_worker.cancel()
        watchdog = getattr(self, "watchdog", None)

        if watchdog is not None:
            watchdog.cancel()

        if self.voice_client.is_playing():
            self._stop_playback()

        workers = [self.synth_worker, self.play_worker]

        if watchdog is not None:
            workers.append(watchdog)

        await asyncio.gather(
            *workers,
            return_exceptions=True,
        )

        if self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)
