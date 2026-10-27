"""入室案内の音声を使い回すためのキャッシュ。

同じ人（ID）・同じ名前・同じ話者なら、一度作った wav をそのまま使う。
毎回 TTS を呼ばないので、入室のたびに生成待ちが入らない。

ファイル名は ``<ユーザーID>-<内容のハッシュ>.wav``。名前に加えて話者も
ハッシュに入れているので、どちらかが変われば別のファイルになる。
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class JoinVoiceCache:
    def __init__(self, directory, *, voice: str = "", limit: int = 500):
        self.directory = Path(directory)
        self.voice = voice
        self.limit = max(10, int(limit))

    def path_for(self, member_id: int, text: str) -> Path:
        """同じ内容なら同じファイルになるパス。"""

        digest = hashlib.sha1(f"{text}\0{self.voice}".encode()).hexdigest()[:16]

        return self.directory / f"{int(member_id)}-{digest}.wav"

    def get(self, member_id: int, text: str) -> str | None:
        """保存済みならそのパスを返す。"""

        path = self.path_for(member_id, text)

        return str(path) if path.is_file() else None

    async def get_or_create(self, member_id: int, text: str, synthesize) -> str | None:
        """保存済みならそれを、無ければ ``synthesize(text)`` で作って保存して返す。"""

        cached = self.get(member_id, text)

        if cached is not None:
            return cached

        try:
            audio = await synthesize(text)
        except Exception:  # noqa: BLE001 作れないときは呼び出し側の通常処理に任せる
            logger.warning("入室案内の音声を作れませんでした: %s", member_id, exc_info=True)
            return None

        if not audio:
            return None

        path = self.path_for(member_id, text)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 途中で落ちても壊れたwavを残さない
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(audio)
            temporary.replace(path)
        except OSError:
            logger.warning("入室案内の音声を保存できませんでした: %s", path, exc_info=True)
            return None

        self.prune()
        logger.info("入室案内の音声を保存: %s", path.name)

        return str(path)

    def prune(self):
        """古い順に消して上限件数に収める。"""

        try:
            files = sorted(
                (item for item in self.directory.glob("*.wav") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return

        for old in files[: max(0, len(files) - self.limit)]:
            try:
                old.unlink()
            except OSError:
                continue
