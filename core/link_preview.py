"""Small link preview fetcher for monitor prompts."""
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

URL_RE = re.compile(r"https?://[^\s<>'\"，。；：！？、（）()【】\[\]{}]+")


class _PreviewHTMLParser(HTMLParser):
    """Extract compact, readable page metadata without third-party parsers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_title = ""
        self.description = ""
        self._tag_stack = []
        self._current_text_tag = ""
        self._title_parts = []
        self._h1_parts = []
        self._paragraphs = []
        self._paragraph_parts = []
        self._body_texts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): v for k, v in attrs if k}
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
        self._tag_stack.append(tag)

        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = attrs.get("content") or ""
            if name in ("description", "og:description", "twitter:description"):
                self.description = self.description or _clean_text(content)
            if name in ("og:title", "twitter:title"):
                self.meta_title = self.meta_title or _clean_text(content)

        if tag in ("title", "h1", "p"):
            self._current_text_tag = tag
            if tag == "p":
                self._paragraph_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self.title = _clean_text(" ".join(self._title_parts))
            self._current_text_tag = ""
        elif tag == "h1":
            self._current_text_tag = ""
        elif tag == "p":
            text = _clean_text(" ".join(self._paragraph_parts))
            if len(text) >= 20 and len(self._paragraphs) < 4:
                self._paragraphs.append(text)
            self._paragraph_parts = []
            self._current_text_tag = ""

        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._current_text_tag == "title":
            self._title_parts.append(text)
        elif self._current_text_tag == "h1":
            self._h1_parts.append(text)
        elif self._current_text_tag == "p":
            self._paragraph_parts.append(text)
        if self._current_text_tag != "title" and len(self._body_texts) < 80:
            self._body_texts.append(text)

    @property
    def h1(self):
        return _clean_text(" ".join(self._h1_parts))

    @property
    def paragraphs(self):
        return self._paragraphs

    @property
    def body_texts(self):
        return self._body_texts


def extract_links(text):
    """Extract unique HTTP(S) links from text, preserving order."""
    links = []
    seen = set()
    for match in URL_RE.findall(str(text or "")):
        url = match.rstrip(".,;:!?，。；：！？、")
        key = url.lower()
        if url and key not in seen:
            links.append(url)
            seen.add(key)
    return links


def is_wechat_record_url(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() != "support.weixin.qq.com":
        return False
    return "favorite_record" in parsed.query or "favorite_record" in parsed.path


def fetch_link_preview(url, timeout=4, max_bytes=256 * 1024):
    """Fetch a compact preview for one public link.

    Returns a dict with status/title/summary. It intentionally does not try to
    read private WeChat favorite/chat-record links because HTTP only exposes a
    generic shell page, not the forwarded record content.
    """
    if is_wechat_record_url(url):
        return {
            "url": url,
            "status": "unavailable",
            "title": "微信聊天记录链接",
            "summary": "HTTP 只能打开微信 Favorites 通用壳，无法读取被转发的聊天记录正文；需要结合前后文判断链接内容。",
        }

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                )
            },
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        data = _read_limited(response, max_bytes)
    except Exception as e:
        return {
            "url": url,
            "status": "error",
            "title": "",
            "summary": f"链接读取失败：{type(e).__name__}",
        }

    if not _is_textual(content_type):
        label = content_type.split(";")[0] or "未知类型"
        return {
            "url": url,
            "status": "unsupported",
            "title": "",
            "summary": f"非文本网页内容：{label}",
        }

    text = _decode_bytes(data, response.encoding)
    if "html" in content_type or "<html" in text[:1000].lower():
        return _preview_html(url, text)
    return {
        "url": url,
        "status": "ok",
        "title": "",
        "summary": _clean_text(text)[:700],
    }


def format_link_previews(previews):
    """Format previews for inclusion in an AI prompt."""
    lines = []
    for idx, item in enumerate(previews, 1):
        status = item.get("status", "")
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        parts = [f"{idx}. {url}"]
        if status and status != "ok":
            parts.append(f"状态：{status}")
        if title:
            parts.append(f"标题：{title}")
        if summary:
            parts.append(f"摘要：{summary}")
        lines.append("\n   ".join(parts))
    return "\n".join(lines)


def _read_limited(response, max_bytes):
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        remaining = max_bytes - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += len(chunk[:remaining])
        if total >= max_bytes:
            break
    return b"".join(chunks)


def _is_textual(content_type):
    if not content_type:
        return True
    base = content_type.split(";")[0].strip()
    return (
        base.startswith("text/")
        or base in ("application/xhtml+xml", "application/xml", "application/json")
    )


def _decode_bytes(data, encoding):
    for enc in (encoding, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return data.decode(enc, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _preview_html(url, text):
    parser = _PreviewHTMLParser()
    try:
        parser.feed(text)
    except Exception:
        pass

    title = parser.meta_title or parser.title or parser.h1
    summary_parts = []
    if parser.description:
        summary_parts.append(parser.description)
    for paragraph in parser.paragraphs:
        if paragraph not in summary_parts:
            summary_parts.append(paragraph)
        if len(" ".join(summary_parts)) >= 700:
            break

    if not summary_parts:
        seen = set()
        for text_part in parser.body_texts:
            text_part = _clean_text(text_part)
            if len(text_part) < 8:
                continue
            if title and text_part == title:
                continue
            key = text_part.lower()
            if key in seen:
                continue
            seen.add(key)
            summary_parts.append(text_part)
            if len(" ".join(summary_parts)) >= 700:
                break

    summary = _clean_text(" ".join(summary_parts))[:700]
    return {
        "url": url,
        "status": "ok" if title or summary else "empty",
        "title": title[:160],
        "summary": summary,
    }


def _clean_text(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text)
    return text.strip()
