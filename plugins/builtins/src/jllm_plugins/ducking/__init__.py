"""人が喋っている間、読み上げの音量をそっと下げる（実験機能）。

- 発話の検知は ``discord-ext-voice-recv``（VoiceRecvClient）で行い、**音が届いている
  時間の長さ**で判定する（音量は見ない）。``DUCK_TRIGGER_SECONDS`` のあいだ続けて
  届いたときだけ「喋っている」とみなすので、短い相づちでは下がらない。
- 既定では音声を **デコードしない**（sink は opus のまま受け取る）。届いたパケットの
  到着だけを見るので、DAVE（E2EE）で暗号化されていても・パケットが壊れていても、
  復号する経路が無いぶん受信ループは止まらない。
- ``decode=True`` にしたときだけ PCM まで復号する。その場合は DAVE の復号
  （:func:`install_dave_support`）と、壊れたパケットで受信ループを止めない
  ``SafeDecoder`` を必ず一緒に入れる。
- **注意**: ``opus`` ロガーの ``N packets were lost being flushed in decoder-XXXX`` は
  ジッタバッファを掃き出したときの警告で、Opus のデコーダが作られた証拠ではない
  （``PacketDecoder`` は ``sink.wants_opus()`` が True なら ``Decoder`` を作らない）。
- 音量は「目標値へ毎tick少しずつ寄せる」ことで、スッと下げてフワッと戻す。
  下げるのは速く、戻すのはゆっくり（急に切り替えないので自然に聞こえる）。
- 継続時間・下げ幅・保持時間・tick は設定（``DUCK_*``）で変えられる。
"""

import asyncio
import logging
import time
from collections import deque

from .duck_curve import MAX_SPEED, MIN_SPEED, DuckCurve, Point, clamp

logger = logging.getLogger(__name__)

try:  # 実験機能なので、無くても読み上げは動く
    from discord.ext import voice_recv

    VOICE_RECV_AVAILABLE = True
except Exception:  # noqa: BLE001
    voice_recv = None
    VOICE_RECV_AVAILABLE = False


#: デバッグログの間隔（秒）
DEBUG_INTERVAL = 3.0


def decrypt_dave_packet(session, user_id, data: bytes) -> bytes | None:
    """DAVE の音声パケットを復号する。

    復号できたらその中身、できないときは ``None``（呼び出し側は元のまま使う）。
    """

    if session is None or not data or user_id is None:
        return None

    if not getattr(session, "ready", False):
        return None

    try:
        from davey import MediaType
    except ImportError:
        return None

    try:
        return session.decrypt(user_id, MediaType.audio, bytes(data))
    except Exception:  # noqa: BLE001 復号できないパケットは元のまま渡す
        logger.debug("DAVEの復号に失敗（そのまま渡す）", exc_info=True)
        return None


def install_dave_support() -> bool:
    """voice-recv が DAVE（E2EE）の音声を復号できるようにする。

    ``decode=True``（PCM まで復号する経路）のときだけ必要になる。デコードしない
    既定の経路では、暗号化されたままのパケットを読まないので復号は要らない。

    できなければ False（その場合も読み上げは動く）。
    """

    if not VOICE_RECV_AVAILABLE:
        return False

    try:
        from discord.ext.voice_recv import opus as vr_opus
    except ImportError:
        return False

    decoder_cls = getattr(vr_opus, "Decoder", None)

    if decoder_cls is None:
        return False

    # 1) 壊れたパケットで受信ループが死なないようにする（無音を返す）
    if not getattr(decoder_cls, "_voice_recv_safe_decode", False):

        class SafeDecoder(decoder_cls):  # type: ignore[misc, valid-type]
            _voice_recv_safe_decode = True

            def decode(self, data, *, fec=False):
                try:
                    return super().decode(data, fec=fec)
                except Exception:  # noqa: BLE001 壊れたパケットは無音にする
                    return b""

        vr_opus.Decoder = SafeDecoder

    # 2) DAVE の復号をはさむ
    packet_decoder = getattr(vr_opus, "PacketDecoder", None)

    if packet_decoder is None or getattr(packet_decoder, "_voice_recv_dave", False):
        return True

    original = packet_decoder._process_packet

    def _process_packet(self, packet):
        data = getattr(packet, "decrypted_data", None)

        if data:
            vc = getattr(getattr(self, "sink", None), "voice_client", None)
            connection = getattr(vc, "_connection", None)
            session = getattr(connection, "dave_session", None)
            user_id = getattr(self, "_cached_id", None)

            decrypted = decrypt_dave_packet(session, user_id, data)

            if decrypted is not None:
                packet.decrypted_data = decrypted

        return original(self, packet)

    packet_decoder._process_packet = _process_packet
    packet_decoder._voice_recv_dave = True

    logger.info("DAVE（音声E2EE）の復号を有効にしました")

    return True


class SpeechTracker:
    """音が届いている時間の長さで「喋っている」を判定する。

    - ``trigger_seconds`` のあいだ続けて届いたときだけ喋っているとみなす
    - 喋り終わって ``hold_seconds`` は下げたままにする
    - ``gap_seconds`` 以内の途切れは「続いている」とみなす
    """

    def __init__(self, hold_seconds: float = 0.8, *, gap_seconds: float = 0.3):
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.gap_seconds = max(0.0, float(gap_seconds))
        self.last_spoke = 0.0
        self.speakers: dict[int, float] = {}
        self.speaking = False
        self.packets = 0
        self._speaking_since = 0.0
        self._last_packet = 0.0

    def observe_packet(self, user_id=None, now: float | None = None) -> bool:
        """音が届いた（＝喋っている）。"""

        now = time.monotonic() if now is None else now

        if self._speaking_since <= 0.0 or now - self._last_packet > self.gap_seconds:
            self._speaking_since = now  # 途切れたら数え直す

        self._last_packet = now
        self.packets += 1
        self.speaking = True
        self.last_spoke = now

        if user_id is not None:
            self.speakers[int(user_id)] = now

        return True

    def speaking_seconds(self, now: float | None = None) -> float:
        """いま続けて喋っている秒数（途切れたら0から数え直す）。"""

        if self._speaking_since <= 0.0:
            return 0.0

        now = time.monotonic() if now is None else now

        if now - self._last_packet > self.gap_seconds:
            return 0.0

        return max(0.0, now - self._speaking_since)

    def receiving(self, now: float | None = None) -> bool:
        """いま音が届いている（喋り始めの、まだ下げていない段階も含む）。"""

        if self._last_packet <= 0.0:
            return False

        now = time.monotonic() if now is None else now

        return now - self._last_packet <= self.gap_seconds

    def touch(self, user_id=None, now: float | None = None) -> None:
        """音量を見ずに「喋った」ことにする（テスト・手動用）。"""

        now = time.monotonic() if now is None else now
        self.speaking = True
        self._speaking_since = now
        self._last_packet = now
        self.last_spoke = now

        if user_id is not None:
            self.speakers[int(user_id)] = now

    def active_speakers(self, now: float | None = None) -> list[int]:
        """いま喋っている（保持の範囲にいる）人だけ。"""

        now = time.monotonic() if now is None else now
        alive = {
            user_id: at for user_id, at in self.speakers.items() if now - at <= self.hold_seconds
        }
        self.speakers = alive

        return sorted(alive)

    def active(self, now: float | None = None) -> bool:
        """いま（直前まで）誰かが喋っているか。"""

        if self.last_spoke <= 0.0:
            return False

        return (now if now is not None else time.monotonic()) - self.last_spoke <= self.hold_seconds

    def reset(self) -> None:
        self.last_spoke = 0.0
        self.speakers.clear()
        self.speaking = False
        self.packets = 0
        self._speaking_since = 0.0
        self._last_packet = 0.0


def make_sink(tracker: SpeechTracker, *, decode: bool = False, on_audio=None, debug: bool = False):
    """受け取った音声の**到着**で「喋っている」を記録する sink。

    既定（``decode=False``）は opus のまま受け取る（``wants_opus()`` は True）。
    音声は復号しないので、DAVE（E2EE）のパケットでも壊れたパケットでも、デコードで
    例外にならない。``decode=True`` のときだけ PCM まで復号し、``on_audio`` へ渡す
    （その場合は :func:`install_dave_support` とセットで使う）。
    """

    if not VOICE_RECV_AVAILABLE:
        return None

    class SpeakingSink(voice_recv.AudioSink):
        def __init__(self):
            super().__init__()
            self.tracker = tracker
            self.debug = bool(debug)

        def wants_opus(self) -> bool:
            # 既定は音声を復号しない（届いたパケットの到着だけで判定する）
            return not decode

        def write(self, user, data):
            if user is None or getattr(user, "bot", False):
                return

            user_id = getattr(user, "id", None)
            self.tracker.observe_packet(user_id)

            if decode and on_audio is not None:
                pcm = getattr(data, "pcm", b"")

                if pcm:
                    on_audio(user_id, pcm)

            if not self.debug:
                return

            now = time.monotonic()

            if now - getattr(self, "last_log", 0.0) >= DEBUG_INTERVAL:
                logger.info(
                    "ducking: 喋っている %s（受け取り中 %s / 話者 %s）",
                    self.tracker.speaking,
                    self.tracker.receiving(),
                    len(self.tracker.speakers),
                )
                self.last_log = now

        def cleanup(self):
            return None

    return SpeakingSink()


#: カーブを残す長さ（秒）
HISTORY_SECONDS = 60.0


def measure_curve(samples: list[tuple], level: float) -> dict:
    """直近の「喋り始め → 下げ → 戻り」の時間を測る（ファンの回転数制御のように見る）。

    samples は ``(時刻, 音量, 音が届いているか, 喋っているか)`` の並び。
    """

    if not samples:
        return {}

    result: dict[str, float] = {}
    floor = level + (1.0 - level) * 0.05

    onsets = [0] if samples[0][2] else []
    onsets += [
        index for index in range(1, len(samples)) if samples[index][2] and not samples[index - 1][2]
    ]

    if onsets:
        start = onsets[-1]
        onset = samples[start][0]
        moved = next((item[0] for item in samples[start:] if item[1] < 0.99), None)
        ducked = next((item[0] for item in samples[start:] if item[1] <= floor), None)

        if moved is not None:
            result["trigger_delay"] = round(moved - onset, 2)

        if moved is not None and ducked is not None:
            result["duck_time"] = round(ducked - moved, 2)

    ends = [
        index for index in range(1, len(samples)) if samples[index - 1][3] and not samples[index][3]
    ]

    if ends:
        end = ends[-1]
        rising = next((item[0] for item in samples[end:] if item[1] > floor), None)
        full = next((item[0] for item in samples[end:] if item[1] >= 0.99), None)

        if rising is not None:
            result["release_start"] = round(rising - samples[end][0], 2)

        if rising is not None and full is not None:
            result["release_time"] = round(full - rising, 2)

    return result


class VolumeDucker:
    """目標の音量へ、なめらかに寄せていく。"""

    def __init__(
        self,
        tracker: SpeechTracker,
        apply,
        *,
        level: float = 0.2,
        tick_seconds: float = 0.04,
        attack: float = 0.25,
        release: float = 0.06,
        curve=None,
    ):
        self.tracker = tracker
        self.apply = apply
        # 区間ごとの音量点（ファンの回転数制御のようなカーブ）
        self.curve = (
            curve
            if curve is not None
            else DuckCurve(
                [Point(0.0, 1.0), Point(1.0, min(1.0, max(0.0, float(level))))],
                attack=attack,
                release=release,
                hold_seconds=getattr(tracker, "hold_seconds", 0.8),
            )
        )
        self.level = self.curve.level
        self.tick_seconds = max(0.01, float(tick_seconds))
        # カーブを渡されたら、速さもカーブの設定を使う（Web UI で一緒に編集する）
        if curve is not None:
            attack = curve.attack
            release = curve.release

        self.attack = min(1.0, max(0.01, float(attack)))
        self.release = min(1.0, max(0.01, float(release)))
        self.volume = 1.0
        self._task: asyncio.Task | None = None
        # (時刻, 音量, 音が届いているか, 喋っているか)
        self.history: deque[tuple[float, float, bool, bool]] = deque(
            maxlen=max(10, int(HISTORY_SECONDS / max(0.01, tick_seconds))),
        )

    def set_curve(self, curve) -> None:
        """カーブを差し替える（Web UI からの編集を、走っている再生に即反映）。"""

        self.curve = curve
        self.level = curve.level
        self.attack = clamp(float(curve.attack), MIN_SPEED, MAX_SPEED)
        self.release = clamp(float(curve.release), MIN_SPEED, MAX_SPEED)

    def target(self, now: float | None = None) -> float:
        """いまの目標音量（喋っている秒数に応じた区間の値）。"""

        now = time.monotonic() if now is None else now

        if not self.tracker.active(now):
            return 1.0

        return self.curve.target(self.tracker.speaking_seconds(now))

    def step(self, ducked: bool, now: float | None = None) -> float:
        """1tickぶん音量を動かして、いまの音量を返す。"""

        if ducked:
            target = self.target(now)
        else:
            target = 1.0

        rate = self.attack if target < self.volume else self.release
        self.volume += (target - self.volume) * rate

        if abs(target - self.volume) < 0.005:
            self.volume = target

        return self.volume

    async def run(self):
        while True:
            now = time.monotonic()
            self.apply(self.step(True, now))
            self.history.append(
                (
                    now,
                    self.volume,
                    self.tracker.receiving(now),
                    self.tracker.active(now),
                )
            )
            await asyncio.sleep(self.tick_seconds)

    def snapshot(self, seconds: float = 30.0, now: float | None = None) -> dict:
        """カーブ表示用の履歴（新しい方から ``seconds`` 秒ぶん）。"""

        now = time.monotonic() if now is None else now
        samples = [item for item in self.history if now - item[0] <= seconds]

        return {
            "seconds": seconds,
            "tick": round(self.tick_seconds, 3),
            "level": self.level,
            "curve": self.curve.to_dict(),
            "trigger_seconds": self.curve.reaction_seconds,
            "hold_seconds": self.curve.hold_seconds,
            "samples": [
                {
                    "ago": round(now - item[0], 2),
                    "volume": round(item[1], 3),
                    "receiving": bool(item[2]),
                    "active": bool(item[3]),
                }
                for item in samples
            ],
            "timings": measure_curve(
                [(item[0], item[1], item[2], item[3]) for item in samples],
                self.level,
            ),
        }

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self.run())

        return self._task

    async def stop(self):
        if self._task is not None:
            self._task.cancel()

            try:
                await self._task
            except asyncio.CancelledError, Exception:  # noqa: BLE001
                pass

            self._task = None

        # 元の音量に戻しておく
        self.volume = 1.0
        self.history.clear()
        self.apply(1.0)


def build_ducker(
    voice_client,
    apply,
    *,
    level: float,
    hold_seconds: float,
    tick_seconds: float = 0.04,
    trigger_seconds: float = 1.0,
    decode: bool = False,
    on_audio=None,
    debug: bool = False,
    curve=None,
):
    """喋り検知つきの ducking を始める。使えないときは ``None``。

    VoiceRecvClient でない（受け取れない）ときは何もしない。
    既定（``decode=False``）は受け取った音声を復号せず、パケットの到着だけで喋りを
    判定する。``decode=True`` のときは PCM まで復号し、その前に DAVE の復号
    （:func:`install_dave_support`）を入れて受信ループが壊れたパケットで死なないようにする。
    """

    if not VOICE_RECV_AVAILABLE or voice_client is None:
        return None

    if not isinstance(voice_client, voice_recv.VoiceRecvClient):
        logger.info(
            "ducking: 受け取り非対応の接続なので使いません（VoiceRecvClient で再接続すると有効）"
        )
        return None

    if decode:
        # デコードする経路だけ DAVE（E2EE）の復号と SafeDecoder が要る
        install_dave_support()

    tracker = SpeechTracker(hold_seconds)
    sink = make_sink(tracker, decode=decode, on_audio=on_audio, debug=debug)

    try:
        voice_client.listen(sink)
    except Exception:  # noqa: BLE001 聞けなくても読み上げは続ける
        logger.warning("ducking: 発話の受け取りを開始できませんでした", exc_info=True)
        return None

    ducker = VolumeDucker(tracker, apply, level=level, tick_seconds=tick_seconds, curve=curve)
    ducker.tracker = tracker
    ducker.start()

    return ducker
