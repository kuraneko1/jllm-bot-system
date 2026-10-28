"""Bounded public-web fetches with article-to-Markdown extraction."""

import ipaddress
import re
import socket

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

# 展開後のHTMLの上限。Wikipedia の記事は 2〜3MB になるため 4MiB まで許す
# （圧縮後の content_length とは別に、読み込んだ累計バイト数でも止める）。
MAX_BYTES = 4 * 1024 * 1024
MAX_PAGE_CHARS = 12000


class WebContentError(ValueError):
    pass


def public_ip(value):
    address = ipaddress.ip_address(value)
    address = (
        address.ipv4_mapped
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped
        else address
    )
    return address.is_global and not address.is_multicast


def validate_url(value):
    try:
        url = URL(value)
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.user is not None
            or url.password is not None
        ):
            raise ValueError()
        if url.port not in (80, 443):
            raise ValueError()
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            pass
        else:
            if not public_ip(url.host):
                raise ValueError()
        return url.with_fragment(None)
    except (ValueError, TypeError) as exc:
        raise WebContentError("公開WebページのHTTP／HTTPS URLを指定してください。") from exc


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not public_ip(address["host"]) for address in addresses):
            raise WebContentError("このURLの接続先は公開Webサーバーではありません。")
        return addresses

    async def close(self):
        await self.resolver.close()


def extract_urls(text):
    # Closing Markdown delimiters and typical Japanese sentence punctuation
    # aren't part of a bare URL. Keep query strings intact.
    found = re.findall(r"https?://[^\s<>`\]「」『』。、，]+", text, re.IGNORECASE)
    urls = []
    for url in found:
        url = url.rstrip(".,;!")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        urls.append(url)
    return list(dict.fromkeys(urls))


def extract_html(html, encoding=None):
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    root = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find(id="main-content")
        or soup.body
        or soup
    )
    for node in root.select(
        'script,style,nav,footer,aside,form,svg,noscript,iframe,button,[hidden],[aria-hidden="true"]'
    ):
        node.decompose()
    parts = []
    blocks = root.select("h1,h2,h3,h4,h5,h6,p,li,pre,blockquote,tr")
    block_ids = {id(node) for node in blocks}
    for node in blocks:
        if any(id(parent) in block_ids for parent in node.parents):
            continue
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if node.name.startswith("h") and node.name[1:].isdigit():
            parts.append("#" * min(int(node.name[1:]), 3) + " " + text)
        elif node.name == "li":
            parts.append("- " + text)
        elif node.name == "pre":
            parts.append("```\n" + node.get_text().strip().replace("```", "``\u200b`") + "\n```")
        elif node.name == "blockquote":
            parts.append("> " + text)
        elif node.name == "tr":
            parts.append(
                " | ".join(
                    cell.get_text(" ", strip=True)
                    for cell in node.find_all(["th", "td"], recursive=False)
                )
            )
        else:
            parts.append(text)
    content = "\n\n".join(parts) if parts else root.get_text("\n", strip=True)
    if len(content.strip()) < 60:
        raise WebContentError(
            "本文を抽出できませんでした。ログインやJavaScriptが必要なページは取得できない場合があります。"
        )
    return title[:300], content


class WebContentFetcher:
    async def fetch(self, value):
        url = validate_url(value)
        resolver = PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "User-Agent": "JLLM-Voice/1.0 (article summary)",
                    "Accept": "text/html,text/plain,application/xhtml+xml",
                },
            ) as session:
                for _ in range(6):
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise WebContentError("リダイレクト先を取得できませんでした。")
                            url = validate_url(str(url.join(URL(location))))
                            continue
                        if response.status != 200:
                            raise WebContentError(
                                f"ページを取得できませんでした（HTTP {response.status}）。"
                            )
                        kind = response.content_type
                        if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
                            raise WebContentError(
                                "HTML／テキストのページだけ要約できます。PDF・画像・動画は対象外です。"
                            )
                        if response.content_length and response.content_length > MAX_BYTES:
                            raise WebContentError("ページが大きすぎます（上限4MiB）。")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise WebContentError("ページが大きすぎます（上限4MiB）。")
                        if kind != "text/plain":
                            title, content = extract_html(bytes(data), response.charset)
                        else:
                            try:
                                text = bytes(data).decode(
                                    response.charset or "utf-8", errors="replace"
                                )
                            except LookupError:
                                text = bytes(data).decode("utf-8", errors="replace")
                            title, content = url.host, text.strip()
                        if not content:
                            raise WebContentError("ページに本文がありませんでした。")
                        return {
                            "url": str(url),
                            "title": title or url.host,
                            "content": content[:MAX_PAGE_CHARS],
                            "truncated": len(content) > MAX_PAGE_CHARS,
                        }
                raise WebContentError("リダイレクトが多すぎてページを取得できませんでした。")
        except WebContentError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WebContentError(
                "ページへの接続に失敗しました（タイムアウト・接続拒否など）。"
            ) from exc
        finally:
            await resolver.close()
