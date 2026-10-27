"""TTS provider と利用側が共有する domain contract。"""

import abc
import math
from dataclasses import dataclass


class TTSBackendError(Exception):
    """合成・話者一覧の取得に失敗したことを表す。"""


class TTSContentError(TTSBackendError):
    """サーバは動いているが、この 1 件の音声が作れなかった（空テイク・無音など）。

    接続の失敗とは違ってエンジン自体は健全なので、多重化しているときも
    「不調」扱いにせず、使っているエンジンも切り替えない。
    """


# VoiceVOX 前提の旧コード向けの別名（非推奨）
VoicevoxError = TTSBackendError


@dataclass(frozen=True)
class Voice:
    """1件の話者。``id`` は :meth:`TTSBackend.synthesize` に渡す値。"""

    id: int | str
    name: str


class TTSBackend(abc.ABC):
    """TTS エンジン側のラッパー。

    実装側の設定は plugin の instance config で注入する。
    コンストラクタはテストから直接叩けるよう素朴な引数にする。
    """

    #: レジストリ上の名前（``@register`` が設定する）
    name: str = ""

    #: 話者一覧のグループ見出し。既定では :attr:`name`
    display_name: str = ""

    #: バックエンド側で速度を決めるときの既定値（話者ごとの設定が無い場合に使う）。
    #: :attr:`ignore_speaker_speed` が真なら常にこの値、偽でも呼び出し側が速度を
    #: 渡さなかった（``None``）ときにこの値を使う。
    default_speed: float = 1.0

    #: 真なら話者ごとの設定（呼び出し側が渡す ``speed``）を無視して、
    #: 常に :attr:`default_speed` で読む。DRAMA3 のようにサーバ側が速度を
    #: 解釈せず、参照音声ごとの Irodori 向け調整を持ち込みたくない場合に使う。
    ignore_speaker_speed: bool = False

    def resolve_speed(self, speed: float | None) -> float:
        """``synthesize`` に渡る速度を、バックエンド側のポリシーで決め直す。

        速度を尊重するエンジンは、この関数を呼ばなければ今までどおり
        呼び出し側の値がそのまま使われる（他の kind の挙動は変えない）。
        バックエンド単位で速度を固定したい kind だけが ``synthesize`` の
        先頭でこれを呼ぶ。
        """

        if self.ignore_speaker_speed:
            return self.default_speed

        if speed is None:
            return self.default_speed

        try:
            value = float(speed)
        except TypeError, ValueError:
            return self.default_speed

        return value if math.isfinite(value) and value > 0 else self.default_speed

    @classmethod
    def from_env(cls, env: dict[str, str]) -> TTSBackend:
        """環境変数からインスタンスを作る。バックエンド側で override する。"""

        return cls()

    def describe(self) -> str:
        """起動ログに出す一行。"""

        return self.name

    async def setup(self) -> None:
        """初回呼び出し。接続確認や話者一覧のプリロードに使う。"""
        return None

    @abc.abstractmethod
    async def list_voices(self) -> list[Voice]:
        """利用可能な話者一覧（表示順）。"""

    @abc.abstractmethod
    async def synthesize(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ) -> bytes:
        """音声を合成して返す（wav 等のバイト列）。

        ``speaker_id`` には :meth:`list_voices` が返した :attr:`Voice.id` を
        そのまま渡す。エンジン固有の識別子（文字列）でもよい。
        """

    async def health(self) -> None:
        """疎通確認。既定は話者一覧が取れるかどうか。

        失敗したら :class:`TTSBackendError` を投げる（多重化したときの
        フォールバック判定に使う）。軽い経路があれば override する。
        """

        await self.list_voices()

    async def close(self) -> None:
        """セッション等の解放。"""
        return None

    async def speakers(self) -> list[dict]:
        """:meth:`list_voices` の VOICEVOX 風ビュー。

        読み上げCog・ダッシュボードはこれを話者一覧として扱う。
        """

        voices = await self.list_voices()

        return [
            {
                "name": self.display_name or self.name,
                "styles": [{"id": voice.id, "name": voice.name} for voice in voices],
            }
        ]
