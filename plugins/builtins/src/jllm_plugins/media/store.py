import asyncio
import json
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaConfig:
    data_file: str = "media.json"
    media_dir: str = "media"
    max_bytes: int = 10 * 1024 * 1024
    max_dim: int = 360
    fps: int = 8
    colors: int = 48
    approver_id: int = 0
    owner_id: int = 0


class MediaStore:
    def __init__(self, storage, config: MediaConfig):
        self.storage = storage
        self.config = config
        self.media_dir = storage.legacy_path(config.media_dir)
        self.pending_dir = self.media_dir / "pending"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.data_path = storage.legacy_path(config.data_file)
        self.media: dict[str, str] = {}
        self.pending: dict[str, dict] = {}
        self._load()

    def _load(self):
        self.media.clear()
        if not self.data_path.exists():
            return
        try:
            data = json.loads(self.data_path.read_text("utf-8"))
        except OSError, ValueError:
            logger.exception("media.json の読み込みに失敗")
            return
        if not isinstance(data, dict):
            return
        for trigger, filename in data.items():
            if not isinstance(filename, str):
                continue
            candidate = Path(filename)
            if candidate.is_absolute() or candidate.name != filename:
                logger.warning("media登録をスキップ: 不正なファイル名 %s", filename)
                continue
            self.media[str(trigger)] = filename

    def save(self):
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_path.write_text(
            json.dumps(self.media, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def media_key(self, phrase: str) -> str | None:
        folded = phrase.casefold()
        return next((key for key in self.media if key.casefold() == folded), None)

    def _compress_gif(self, source: Path, target: Path):
        target.unlink(missing_ok=True)
        dim = max(2, self.config.max_dim)
        fps = max(1, self.config.fps)
        colors = min(256, max(2, self.config.colors))
        vf = (
            f"fps={fps},scale={dim}:{dim}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:flags=lanczos,split[s0][s1];"
            f"[s0]palettegen=max_colors={colors}:stats_mode=diff[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle[v]"
        )
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-filter_complex",
                vf,
                "-map",
                "[v]",
                "-an",
                "-loop",
                "0",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            detail = (result.stderr or "ffmpeg failed").strip()[-400:]
            raise RuntimeError(detail)

    async def approve(self, request_id: str) -> str:
        request = self.pending.pop(request_id, None)
        if request is None:
            return "このメディア登録申請は期限切れです。"
        source = Path(request["path"])
        if not await asyncio.to_thread(source.is_file):
            return "登録用GIFが見つかりません。"
        phrase = request["phrase"]
        safe = re.sub(r"[^\w\-]+", "_", phrase, flags=re.UNICODE).strip("_") or "media"
        target = self.media_dir / f"{safe}-{request_id}.gif"
        temporary = self.media_dir / f".{target.name}.tmp.gif"
        try:
            await asyncio.to_thread(self._compress_gif, source, temporary)
            temporary.replace(target)
            await asyncio.to_thread(source.unlink, missing_ok=True)
        except Exception as exc:
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            await asyncio.to_thread(source.unlink, missing_ok=True)
            logger.exception("GIF圧縮に失敗: %s", exc)
            return "GIFの圧縮に失敗したため登録できませんでした。"
        old_key = self.media_key(phrase)
        if old_key is not None and old_key != phrase:
            del self.media[old_key]
        self.media[phrase] = target.name
        self.save()
        return f"「{phrase}」のGIFを登録しました。"

    async def reject(self, request_id: str) -> str:
        request = self.pending.pop(request_id, None)
        if request is not None:
            await asyncio.to_thread(Path(request["path"]).unlink, missing_ok=True)
        return "メディア登録申請を拒否しました。"

    async def expire(self, request_id: str):
        request = self.pending.pop(request_id, None)
        if request is not None:
            await asyncio.to_thread(Path(request["path"]).unlink, missing_ok=True)

    def register(
        self, phrase: str, data: bytes, requester_id: int, channel_id: int, message_id: int
    ) -> str:
        request_id = uuid.uuid4().hex[:8]
        pending_path = self.pending_dir / f"{request_id}.gif"
        pending_path.write_bytes(data)
        self.pending[request_id] = {
            "path": str(pending_path),
            "phrase": phrase,
            "requester_id": requester_id,
            "channel_id": channel_id,
            "message_id": message_id,
        }
        return request_id


class Plugin:
    async def setup(self, ctx):
        config = MediaConfig(
            data_file=ctx.config.get("data_file", "media.json"),
            media_dir=ctx.config.get("media_dir", "media"),
            max_bytes=ctx.config.get("max_bytes", 10 * 1024 * 1024),
            max_dim=ctx.config.get("max_dim", 360),
            fps=ctx.config.get("fps", 8),
            colors=ctx.config.get("colors", 48),
            approver_id=ctx.config.get("approver_id", 0),
            owner_id=ctx.config.get("owner_id", 0),
        )
        self.store = MediaStore(ctx.dependencies["storage.local"], config)
        return {"dictionary.media": self.store}

    async def start(self):
        pass

    async def stop(self):
        pass
