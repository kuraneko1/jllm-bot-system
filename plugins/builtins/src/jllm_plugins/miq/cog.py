# ruff: noqa: E501, E402, E741, B905, B007, E731, UP031
"""miq コマンドで、Discord の投稿を Make it a Quote 風の画像にする。

- 発火: 本文が miq のコマンドのとき（self.config.words で変更可）
  - `miq` … 返信先を1件
  - `miqmiq` `miqmiqmiq` … その人の直前の発言もまとめる（最大10件）
  - `miq12` … 構図の番号指定（`miq台帳` で一覧）
  - `miq 縦` / `miq 正方形` / `miq 横` … 比率、`miq cute` … 系統
  - `miq vs` / `miq love` / `miq talk` … 返信相手との掛け合い
  - `miq台帳` … 構図の一覧画像、`miq help` … 使い方（どちらも返信不要）
- 引用する内容: 本文・表示名・ユーザー名・アイコン（添付画像があればコラージュ）
- 生成: services/miq/（Pillowと同梱フォント。描画時に通信しない。GPUも外部APIも不要）
- サーバー絵文字（<:name:id>）はCDNから画像を取って貼る（一度取ったら保存して再利用）
- サイズ: self.config.size（既定はSD 640x360）。縦は MIQ_PORTRAIT_SIZE、正方形は MIQ_SQUARE_SIZE
- 履歴: 1回ごとに data/miq.jsonl へ追記し、journal にも1行残す
"""

import asyncio
import io
import json
import logging
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from PIL import Image


@dataclass
class MiqConfig:
    emoji_cache_dir: str = "data/miq_emojis"
    emoji_cache_limit: int = 500
    layout: str = "auto"
    log_path: str = "data/miq.jsonl"
    max_chars: int = 2000
    size: str = "640x360"
    text_align: str = "right"
    words: tuple[str, ...] = ("miq",)


from jllm_plugins.miq import compose, default_size, find_layout, miq_catalog, resolve_size, warmup
from jllm_plugins.miq.command import parse as parse_command
from jllm_plugins.miq.command import usage
from jllm_plugins.miq.gather import SCAN_LIMIT, conversation, same_author
from jllm_plugins.miq.miq_dialogue import MOODS, Speaker, compose_dialogue
from jllm_plugins.miq.miq_fonts import FAMILIES
from jllm_plugins.miq.miq_images import collage, encode_png, spoiler
from jllm_plugins.miq.miq_layouts import REGISTRY
from jllm_plugins.miq.trigger import MIQ_FILE_PREFIX, is_miq_image
from jllm_plugins.restrictions.service import UserRestrictions

logger = logging.getLogger(__name__)

#: 本文中のサーバー絵文字（<:name:id> / アニメは <a:name:id>）
CUSTOM_EMOJI = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")

#: ユーザーメンション（<@123> / ニックネーム付きは <@!123>）
MENTION = re.compile(r"<@!?(\d+)>")

#: 画像生成の同時実行（CPUを使うので抑える）
MAX_CONCURRENT = 2

#: 直近に処理したメッセージ（再接続で同じメッセージが再配信されても二度作らない）
RECENT_LIMIT = 200

#: 履歴ファイルの行数上限（超えたら古い方を捨てる）
HISTORY_LIMIT = 1000
HISTORY_KEEP = 500

#: 1枚に使う添付画像の上限（枚数・合計バイト・1枚のバイト・ピクセル数）
MAX_MEDIA = 4
MAX_MEDIA_BYTES = 24 * 1024 * 1024
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 12_000_000

#: 添付画像として貼れる形式
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def describe(command) -> str:
    """履歴に残す短い説明（どの指定で描いたか）。"""

    bits = []

    if command.count > 1:
        bits.append(f"count={command.count}")
    if command.layout is not None:
        bits.append(f"layout={command.layout}")
    if command.orientation:
        bits.append(f"orientation={command.orientation}")
    if command.category:
        bits.append(f"category={command.category}")
    if command.mood:
        bits.append(f"mood={command.mood}")

    return " ".join(bits)


class MiqError(Exception):
    """利用者に見せてよい失敗（アイコンが取れない等）。"""


def icon_size(px: int) -> int:
    """DiscordのアイコンURLに渡せるサイズへ丸める。

    2の累乗の 16〜4096 しか受け付けない（そうでないと
    ``size must be a power of 2 between 16 and 4096`` で失敗する）。
    """

    size = 16

    while size < int(px) and size < 4096:
        size *= 2

    return min(4096, max(16, size))


class MiqCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, history_path=None, emoji_dir=None, config=None, restrictions=None
    ):
        self.bot = bot
        self.config = config or MiqConfig()
        self.restrictions = restrictions or getattr(bot, "restrictions", None) or UserRestrictions()
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.recent: deque[int] = deque(maxlen=RECENT_LIMIT)
        self.history_path = Path(history_path) if history_path else Path(self.config.log_path)
        self.emoji_dir = Path(emoji_dir) if emoji_dir else Path(self.config.emoji_cache_dir)
        self._session: aiohttp.ClientSession | None = None
        # 構図台帳は初回だけ描いて使い回す（合計1.3MBほど）
        self._catalog: list | None = None
        self._catalog_lock = asyncio.Lock()

    async def cog_load(self):
        """初回の描画レイテンシを減らすため、フォント・辞書・各構図を先に温める。

        同梱フォントが欠けていても起動は止めない（描画時に改めて失敗を報告する）。
        """

        try:
            await asyncio.to_thread(warmup)
        except Exception:  # noqa: BLE001 起動を止めないことを優先する
            logger.exception("miq: 事前読み込みに失敗しました（描画時に再試行します）")

    async def cog_unload(self):
        if self._session is not None:
            await self._session.close()

    # --------------------------------------------------- サーバー絵文字

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "jllm-voice-miq"},
            )

        return self._session

    async def custom_emojis(self, text: str) -> dict[str, str]:
        """本文のサーバー絵文字を集めて、画像のパスを返す（取得できたものだけ）。"""

        tokens = {}
        animated = {}

        for match in CUSTOM_EMOJI.finditer(text or ""):
            tokens[match.group(3)] = match.group(2)
            animated[match.group(3)] = bool(match.group(1))

        found: dict[str, str] = {}

        for emoji_id, name in tokens.items():
            path = self.emoji_dir / f"{emoji_id}.png"

            if path.is_file():
                found[emoji_id] = str(path)
                continue

            fetched = await self._download_emoji(emoji_id, animated.get(emoji_id, False))

            if fetched is not None:
                found[emoji_id] = fetched
            else:
                logger.info("miq: サーバー絵文字を取得できません: %s(%s)", name, emoji_id)

        return found

    async def _download_emoji(self, emoji_id: str, animated: bool) -> str | None:
        """CDN から絵文字画像を取って保存する（アニメは1コマ目だけ使う）。"""

        suffix = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{suffix}?size=128"

        try:
            async with self._http().get(url) as response:
                if response.status != 200:
                    return None

                data = await response.read()
        except Exception:  # noqa: BLE001 絵文字が取れなくても画像は作る
            logger.warning("miq: 絵文字の取得に失敗: %s", emoji_id, exc_info=True)
            return None

        path = self.emoji_dir / f"{emoji_id}.png"

        try:
            self.emoji_dir.mkdir(parents=True, exist_ok=True)
            # アニメGIFは1コマ目だけPNGにする（貼るのは静止画）
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                image.convert("RGBA").save(path, format="PNG")
        except OSError, ValueError, ImportError:
            logger.warning("miq: 絵文字を画像にできません: %s", emoji_id, exc_info=True)
            return None

        self._prune_emojis()

        return str(path)

    def _prune_emojis(self):
        """古い絵文字画像を消して上限に収める。"""

        try:
            files = sorted(
                (item for item in self.emoji_dir.glob("*.png") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return

        for old in files[: max(0, len(files) - self.config.emoji_cache_limit)]:
            try:
                old.unlink()
            except OSError:
                continue

    # --------------------------------------------------------------- 入口

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        command = parse_command(message.content)

        if command is None:
            return

        # まるごと無視にされている人の投稿は扱わない
        guild = getattr(message, "guild", None)

        if self.restrictions.ignored(
            getattr(guild, "id", None),
            message.author.id,
        ):
            return

        if message.id in self.recent:
            return

        self.recent.append(message.id)

        word = self.config.words[0]

        if command.kind == "help":
            await self._notice(message, usage())
            return

        if command.kind == "catalog":
            await self._send_catalog(message)
            return

        if message.reference is None:
            await self._notice(
                message,
                f"誰かのメッセージに返信して `{word}` と送ると、その内容を画像にします。\n"
                f"使い方は `{word} help`。",
            )
            return

        target = await self._resolve_target(message)

        if target is None:
            await self._notice(
                message, "返信先のメッセージを取得できませんでした（削除された可能性があります）。"
            )
            return

        if is_miq_image(target):
            # 生成済みの画像に返信してきたときは黙って見送る
            logger.info("miq: 生成済みの画像への返信なので何もしない（%s）", message.id)
            self._record(message, target, text="", ok=False, error="返信先がmiq画像")
            return

        if command.layout is not None:
            try:
                find_layout(command.layout)
            except ValueError:
                await self._notice(
                    message,
                    f"構図 {command.layout} 番はありません（`{word}台帳` で一覧）。",
                )
                return

        if command.mood and command.orientation == "square":
            await self._notice(message, "掛け合いは横か縦だけに対応しています。")
            return

        detail = describe(command)

        async with self.semaphore:
            async with message.channel.typing():
                try:
                    path, size, caption, text = await self._build(message, command, target)
                except MiqError as exc:
                    self._record(message, target, text="", ok=False, error=str(exc), command=detail)
                    await self._notice(message, str(exc))
                    return
                except ValueError as exc:
                    # フォントが見つからない・self.config.size の書式ミスなど、設定側の問題
                    self._record(message, target, text="", ok=False, error=str(exc), command=detail)
                    await self._notice(message, str(exc))
                    return
                except Exception:  # noqa: BLE001 生成失敗で bot を落とさない
                    logger.exception("miq 画像の生成に失敗")
                    self._record(
                        message, target, text="", ok=False, error="生成に失敗", command=detail
                    )
                    await self._notice(message, "画像を作れませんでした。")
                    return

                try:
                    await message.reply(
                        content=caption or None,
                        file=discord.File(path, filename=path.name),
                        mention_author=False,
                    )
                except discord.HTTPException as exc:
                    logger.exception("miq 画像を送信できませんでした")
                    self._record(
                        message,
                        target,
                        text=text,
                        ok=False,
                        error=f"送信できません: {exc.status}",
                        command=detail,
                    )
                else:
                    self._record(message, target, text=text, ok=True, size=size, command=detail)
                finally:
                    path.unlink(missing_ok=True)

    # --------------------------------------------------------------- 台帳

    async def _catalog_pages(self):
        """構図台帳の (ファイル名, PNG) を作る（初回だけ描いて使い回す）。"""

        if self._catalog is None:
            async with self._catalog_lock:
                if self._catalog is None:
                    self._catalog = await asyncio.to_thread(miq_catalog.pages)

        return self._catalog

    async def _send_catalog(self, message: discord.Message):
        async with self.semaphore:
            async with message.channel.typing():
                try:
                    pages = await self._catalog_pages()
                except Exception:  # noqa: BLE001 台帳が作れなくても bot は止めない
                    logger.exception("miq: 台帳を作れませんでした")
                    await self._notice(
                        message, "台帳を作れませんでした。少し待ってもう一度どうぞ。"
                    )
                    return

                files = [discord.File(io.BytesIO(png), filename=name) for name, png in pages]

                try:
                    await message.reply(files=files, mention_author=False)
                except discord.HTTPException:
                    logger.exception("miq: 台帳を送信できませんでした")

    # --------------------------------------------------------------- 中身

    async def _resolve_target(self, message: discord.Message):
        """返信先のメッセージを取る（キャッシュに無ければ取りに行く）。

        削除済みの返信先は author を持たないので、取得できなかった扱いにする。
        """

        reference = message.reference

        if reference is None:
            return None

        resolved = reference.resolved

        if resolved is not None and hasattr(resolved, "author"):
            return resolved

        if reference.message_id is None:
            return None

        try:
            return await message.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return None

    async def _groups(self, message: discord.Message, command, target):
        """1枚にまとめるメッセージの組（掛け合いなら2組）。"""

        def history(anchor):
            return message.channel.history(limit=SCAN_LIMIT, before=anchor)

        if command.mood:
            pair = await conversation(target, history, self._resolve_target)

            if pair is not None:
                return list(pair)

        return [await same_author(target, command.count, history)]

    async def _text(self, group) -> str:
        """その人の発言を1つの本文にまとめる（メンションは名前に直す）。"""

        parts = []

        for message in group:
            body = await resolve_mentions((message.content or "").strip(), message)

            if body:
                parts.append(body)

        return "\n".join(parts)

    async def _media(self, group) -> list:
        """添付画像を集める（枚数・バイト数・ピクセル数の上限つき）。"""

        images: list = []
        total = 0

        for message in group:
            for attachment in getattr(message, "attachments", None) or []:
                if len(images) >= MAX_MEDIA:
                    return images

                content_type = (getattr(attachment, "content_type", "") or "").split(";")[0]

                if content_type not in IMAGE_TYPES:
                    continue

                size = int(getattr(attachment, "size", 0) or 0)

                if size > MAX_FILE_BYTES:
                    logger.info("miq: 添付画像が大きすぎるので飛ばす（%d bytes）", size)
                    continue

                try:
                    data = await attachment.read()
                except Exception:  # noqa: BLE001 取れない画像は飛ばす
                    logger.warning("miq: 添付画像を取得できません", exc_info=True)
                    continue

                if total + len(data) > MAX_MEDIA_BYTES:
                    continue

                try:
                    with Image.open(io.BytesIO(data)) as opened:
                        if opened.width * opened.height > MAX_PIXELS:
                            logger.info("miq: 添付画像のピクセル数が多すぎるので飛ばす")
                            continue
                        if getattr(opened, "is_animated", False):
                            opened.seek(0)  # GIF / APNG / アニメWebP は1コマ目
                        image = opened.convert("RGBA")
                except OSError, ValueError, Image.DecompressionBombError:
                    continue

                total += len(data)
                hidden = getattr(attachment, "is_spoiler", lambda: False)()

                images.append(spoiler(image) if hidden else image)

        return images

    async def _avatar(self, author, directory: Path) -> Path:
        """アイコンを落として保存する（SDなら512pxで足りる）。"""

        path = directory / f"avatar-{author.id}.png"
        width, _ = resolve_size(self.config.size)
        wanted = icon_size(min(1024, max(256, round(970 * width / 1920))))

        try:
            path.write_bytes(await author.display_avatar.replace(size=wanted, format="png").read())
        except Exception as exc:  # noqa: BLE001 アイコンが取れない原因は様々
            logger.warning("miq: アイコンを取得できません: %s", exc)
            raise MiqError("アイコンを取得できませんでした。") from exc

        return path

    async def _picture(self, author, images, directory: Path):
        """(画像のパス, 添付画像として描くか) を返す。"""

        if images:
            path = directory / f"media-{author.id}.png"
            path.write_bytes(encode_png(collage(images), 3))

            return path, True

        return await self._avatar(author, directory), False

    async def _build(self, message: discord.Message, command, target):
        """引用画像を作る（(パス, サイズ表記, キャプション, 本文) を返す）。"""

        groups = await self._groups(message, command, target)
        directory = Path(tempfile.mkdtemp(prefix="miq-"))
        parts = []

        for group in groups:
            parts.append((group, await self._media(group), await self._text(group)))

        text = "\n".join(body for _, _, body in parts if body)
        has_media = any(images for _, images, _ in parts)

        if not text.strip() and not has_media:
            raise MiqError(
                "返信先に本文がありません（画像やスタンプだけのメッセージには使えません）。"
            )

        if len(text) > self.config.max_chars:
            raise MiqError(f"本文が長すぎます（{self.config.max_chars}文字まで）。")

        # サーバー絵文字はCDNから取っておく（無ければ名前を文字で出す）
        custom = await self.custom_emojis(text)
        output = directory / f"{MIQ_FILE_PREFIX}{getattr(target.author, 'id', '')}.png"

        if command.mood:
            speakers = []

            for group, images, body in parts:
                author = group[-1].author
                source, attachment = await self._picture(author, images, directory)
                speakers.append(
                    Speaker(
                        source,
                        body or "（画像）",
                        await server_name(group[-1], author),
                        getattr(author, "name", ""),
                        attachment,
                        custom_emojis=custom,
                        date=getattr(group[-1], "created_at", None),
                    )
                )

            canvas, mood, face = await asyncio.to_thread(
                compose_dialogue,
                *speakers[:2],
                command.mood,
                orientation=command.orientation or "landscape",
                custom_emojis=custom,
            )
            caption = f"-# {MOODS[mood][0]} · {FAMILIES[face].label}"
            output.write_bytes(encode_png(canvas, 3))
        else:
            group, images, body = parts[0]
            author = group[-1].author
            source, attachment = await self._picture(author, images, directory)
            layout = (
                command.layout if command.layout is not None else (self.config.layout or "auto")
            )
            size = None

            if layout == "auto" and command.orientation:
                size = default_size(command.orientation)

            canvas, chosen, face = await asyncio.to_thread(
                compose,
                source,
                body or "（画像）",
                await server_name(group[-1], author),
                getattr(author, "name", ""),
                None,
                None,
                size,
                None,
                None,
                custom,
                self.config.text_align,
                layout=layout,
                category=command.category,
                attachment=attachment,
                date=getattr(group[-1], "created_at", None),
            )
            spec = REGISTRY[chosen]
            caption = f"-# #{spec.number} {spec.label} · {FAMILIES[face].label}"
            output.write_bytes(encode_png(canvas, 3))

        with Image.open(output) as drawn:
            label = f"{drawn.width}x{drawn.height}"

        return output, label, caption, text

    async def _notice(self, message: discord.Message, text: str):
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            logger.exception("miq の案内を送信できませんでした")

    # --------------------------------------------------------------- 履歴

    def _record(
        self,
        message: discord.Message,
        target: discord.Message,
        *,
        text: str,
        ok: bool,
        error: str = "",
        size: str = "",
        command: str = "",
    ):
        """1回の呼び出しを journal と ``data/miq.jsonl`` に残す。"""

        author = getattr(target, "author", None)
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ok": ok,
            "guild": str(getattr(getattr(message, "guild", None), "id", "") or ""),
            "channel": str(getattr(getattr(message, "channel", None), "id", "") or ""),
            "requester": str(getattr(message.author, "name", "")),
            "requester_id": str(getattr(message.author, "id", "")),
            "target": str(getattr(author, "name", "")),
            "target_id": str(getattr(author, "id", "")),
            "chars": len(text),
            "size": size,
            "command": command,
            "error": error,
        }

        logger.info(
            "miq %s: %s が %s を引用（%d文字, %s）%s",
            "ok" if ok else "失敗",
            entry["requester"] or "?",
            entry["target"] or "?",
            entry["chars"],
            size or "-",
            f" / {error}" if error else "",
        )

        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

            self._trim_history()
        except OSError:
            logger.warning("miq の履歴を書けませんでした: %s", self.history_path)

    def _trim_history(self):
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        if len(lines) <= HISTORY_LIMIT:
            return

        self.history_path.write_text(
            "\n".join(lines[-HISTORY_KEEP:]) + "\n",
            encoding="utf-8",
        )


async def resolve_mentions(text: str, message: discord.Message) -> str:
    """本文の ``<@123>`` / ``<@!123>`` を、そのサーバーで見えている名前に置き換える。

    返信先のメッセージに含まれるメンションだけを対象にする（``mentions``）。
    名前が取れないときは元の表記のまま残す。
    """

    mentioned = list(getattr(message, "mentions", None) or [])

    if not mentioned or "<@" not in (text or ""):
        return text

    names: dict[str, str] = {}

    for user in mentioned:
        names[str(getattr(user, "id", ""))] = await server_name(message, user)

    def replace(match: re.Match) -> str:
        name = names.get(match.group(1))

        return f"@{name}" if name else match.group(0)

    return MENTION.sub(replace, text)


async def server_name(message: discord.Message, author) -> str:
    """**そのサーバーで見えている名前**を返す。

    ``discord.User`` の ``display_name`` は全体（グローバル）のプロフィール名なので、
    サーバーごとのニックネームがある人の名前が漏れてしまう。投稿者の Member を
    取り直して、ニックネームを優先する。
    """

    guild = getattr(message, "guild", None) or getattr(author, "guild", None)

    if guild is not None and hasattr(guild, "get_member"):
        member = guild.get_member(getattr(author, "id", None))

        if member is None and hasattr(guild, "fetch_member"):
            try:
                member = await guild.fetch_member(author.id)
            except discord.HTTPException:
                member = None

        if member is not None:
            author = member

    return getattr(author, "display_name", "") or getattr(author, "name", "")


async def setup(bot: commands.Bot):
    await bot.add_cog(MiqCog(bot))
