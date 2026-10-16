"""Reading public web pages and PDFs (ported from providers/web_page_query.py)."""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests

MAX_PAGE_BYTES = 1_000_000
MAX_PDF_BYTES = 20_000_000
MAX_PAGE_CHARS = 30_000
MAX_PDF_CHARS = 60_000
MAX_LINKS = 100
MAX_REDIRECTS = 3
SUPPORTED = {"application/json", "application/pdf", "application/xhtml+xml", "text/html", "text/plain"}
IGNORED = {"script", "style", "noscript", "svg", "template"}


class PageUnavailable(RuntimeError):
    pass


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored = self._in_title = 0
        self._href, self._label = None, []
        self.title, self.text, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in IGNORED:
            self._ignored += 1
        if tag == "title":
            self._in_title += 1
        if tag == "a":
            self._href, self._label = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in IGNORED and self._ignored:
            self._ignored -= 1
        if tag == "title" and self._in_title:
            self._in_title -= 1
        if tag == "a" and self._href:
            self.links.append((" ".join(self._label).strip(), self._href))
            self._href, self._label = None, []

    def handle_data(self, data):
        if self._ignored:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text.append(text)
        if self._in_title:
            self.title.append(text)
        if self._href:
            self._label.append(text)


def validated_url(url: str) -> str:
    """Only public HTTP(S) on standard ports; local and private networks are refused (no SSRF)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PageUnavailable("Only public HTTP(S) links are supported")
    if parsed.username or parsed.password:
        raise PageUnavailable("Links containing credentials are not supported")
    try:
        port = parsed.port
    except ValueError as error:
        raise PageUnavailable("The link has an invalid port") from error
    if port not in {None, 80, 443}:
        raise PageUnavailable("Only standard web ports are supported")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise PageUnavailable("Local network links are not supported")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))}
        except socket.gaierror as error:
            raise PageUnavailable(f"Could not resolve {host}") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PageUnavailable("Local or non-public network links are not supported")
    return parsed.geturl()


def fetch(url: str, session=None) -> dict:
    """Readable text, title and links of one page. Redirects are re-validated hop by hop."""
    session = session or requests
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        current = validated_url(current)
        response = session.get(current, headers={"User-Agent": "DiscordStudyBot/1.0 (+public page reader)"},
                               allow_redirects=False, stream=True, timeout=(5, 20))
        try:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if hop == MAX_REDIRECTS or not location:
                    raise PageUnavailable("The page redirected too many times or invalidly")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            is_pdf = content_type == "application/pdf" or urlparse(current).path.lower().endswith(".pdf")
            if content_type not in SUPPORTED and not is_pdf:
                raise PageUnavailable(f"Unsupported page type: {content_type or 'unknown'}")
            limit = MAX_PDF_BYTES if is_pdf else MAX_PAGE_BYTES
            if int(response.headers.get("Content-Length") or 0) > limit:
                raise PageUnavailable("The page is too large to read")
            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > limit:
                    raise PageUnavailable("The page is too large to read")
            title, links = "", []
            if is_pdf:
                text, title = read_pdf(bytes(body))
            else:
                decoded = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                if content_type in {"text/html", "application/xhtml+xml"}:
                    parser = ReadableHTML()
                    parser.feed(decoded)
                    title, text = " ".join(parser.title).strip(), "\n".join(parser.text)
                    links = public_links(parser.links, current)
                else:
                    text = decoded
            text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            if not text:
                raise PageUnavailable("The page did not contain readable text")
            return {"url": current, "title": title or urlparse(current).hostname or "page",
                    "text": text[:MAX_PDF_CHARS if is_pdf else MAX_PAGE_CHARS], "content_type": content_type,
                    "links": links}
        finally:
            response.close()
    raise PageUnavailable("The page redirected too many times")


def read_pdf(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(BytesIO(data))
        title = str((reader.metadata or {}).get("/Title") or "").strip()
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= MAX_PDF_CHARS:
                break
        return "\n\n".join(parts), title
    except Exception as error:
        raise PageUnavailable(f"The PDF could not be read: {error}") from error


def public_links(raw, page_url: str) -> list[dict]:
    links, seen = [], set()
    for label, href in raw:
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"title": label or absolute, "url": absolute})
        if len(links) >= MAX_LINKS:
            break
    return links
