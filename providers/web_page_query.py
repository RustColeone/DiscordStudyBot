import ipaddress
from html.parser import HTMLParser
import socket
from urllib.parse import urljoin, urlparse

import requests


MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_CHARS = 30_000
MAX_REDIRECTS = 3
SUPPORTED_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}


class WebPageUnavailable(RuntimeError):
    pass


class _ReadableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._title_depth = 0
        self.title_parts = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data):
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text_parts.append(text)
        if self._title_depth:
            self.title_parts.append(text)


def _validated_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebPageUnavailable("Only public HTTP(S) links are supported")
    if parsed.username or parsed.password:
        raise WebPageUnavailable("Links containing credentials are not supported")
    try:
        port = parsed.port
    except ValueError as error:
        raise WebPageUnavailable("The link has an invalid port") from error
    if port not in {None, 80, 443}:
        raise WebPageUnavailable("Only standard web ports are supported")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise WebPageUnavailable("Local network links are not supported")
    try:
        addresses = {str(ipaddress.ip_address(hostname))}
    except ValueError:
        try:
            addresses = {
                result[4][0]
                for result in socket.getaddrinfo(hostname, port or (443 if parsed.scheme == "https" else 80))
            }
        except socket.gaierror as error:
            raise WebPageUnavailable(f"Could not resolve {hostname}") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise WebPageUnavailable("Local or non-public network links are not supported")
    return parsed.geturl()


def fetch_web_page(url: str) -> dict:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        current_url = _validated_url(current_url)
        response = requests.get(
            current_url,
            headers={"User-Agent": "DiscordStudyBot/1.0 (+public page reader)"},
            allow_redirects=False,
            stream=True,
            timeout=(5, 20),
        )
        try:
            if response.is_redirect or response.is_permanent_redirect:
                if redirect_count == MAX_REDIRECTS:
                    raise WebPageUnavailable("The page redirected too many times")
                location = response.headers.get("Location")
                if not location:
                    raise WebPageUnavailable("The page returned an invalid redirect")
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in SUPPORTED_CONTENT_TYPES:
                raise WebPageUnavailable(
                    f"Unsupported page type: {content_type or 'unknown'}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_PAGE_BYTES:
                raise WebPageUnavailable("The page is too large to read")

            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise WebPageUnavailable("The page is too large to read")
            encoding = response.encoding or "utf-8"
            decoded = bytes(body).decode(encoding, errors="replace")
            if content_type in {"text/html", "application/xhtml+xml"}:
                parser = _ReadableHTMLParser()
                parser.feed(decoded)
                title = " ".join(parser.title_parts).strip()
                text = "\n".join(parser.text_parts)
            else:
                title = ""
                text = decoded
            text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            if not text:
                raise WebPageUnavailable("The page did not contain readable text")
            return {
                "url": current_url,
                "title": title or parsed_page_title(current_url),
                "text": text[:MAX_PAGE_CHARS],
                "content_type": content_type,
            }
        finally:
            response.close()

    raise WebPageUnavailable("The page redirected too many times")


def parsed_page_title(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or "Linked page"