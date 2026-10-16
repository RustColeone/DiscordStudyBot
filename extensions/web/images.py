"""Finding one supplementary image on the web (ported from providers/image_search.py)."""

from __future__ import annotations

import os
import tempfile
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests

from .pages import MAX_REDIRECTS, PageUnavailable, validated_url

MAX_CANDIDATES = 8
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 1800
DOWNLOAD_SECONDS = 60
SEARCH_SECONDS = 120
HEADERS = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
           "User-Agent": "DiscordStudyBot/1.0 (+image search)"}


class ImageUnavailable(RuntimeError):
    pass


def download(url: str, max_bytes: int = MAX_DOWNLOAD_BYTES, session=None) -> bytes:
    """Bytes of one public image. Every redirect hop is re-validated, so private networks stay unreachable."""
    session = session or requests
    current, started = url, time.monotonic()
    for hop in range(MAX_REDIRECTS + 1):
        current = validated_url(current)
        response = session.get(current, headers=HEADERS, allow_redirects=False, stream=True, timeout=(10, 60))
        try:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if hop == MAX_REDIRECTS or not location:
                    raise PageUnavailable("The image redirected too many times or invalidly")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            if int(response.headers.get("Content-Length") or 0) > max_bytes:
                raise ImageUnavailable("The image is too large")
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ImageUnavailable("The image is too large")
                if time.monotonic() - started > DOWNLOAD_SECONDS:
                    raise ImageUnavailable("The image download took too long")
            if not body:
                raise ImageUnavailable("The downloaded image is empty")
            return bytes(body)
        finally:
            response.close()
    raise PageUnavailable("The image redirected too many times")


def save_image(data: bytes, directory: Path, max_file_bytes: int) -> tuple[Path, str]:
    """Normalize to a PNG of at most MAX_DIMENSION pixels (JPEG if the PNG would not fit the upload limit)."""
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as source:
            source.load()
            image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    except Exception as error:
        raise ImageUnavailable("The result was not a supported image") from error
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="reference-", suffix=".png", dir=directory)
    os.close(descriptor)
    path = Path(name)
    try:
        image.save(path, format="PNG", optimize=True)
        if path.stat().st_size <= max_file_bytes:
            return path, "image/png"
        jpeg = path.with_suffix(".jpg")
        image.convert("RGB").save(jpeg, format="JPEG", quality=85, optimize=True)
        path.unlink(missing_ok=True)
        if jpeg.stat().st_size > max_file_bytes:
            jpeg.unlink(missing_ok=True)
            raise ImageUnavailable("The image is too large to send here")
        return jpeg, "image/jpeg"
    except Exception:
        path.unlink(missing_ok=True)
        raise


def locate_image(query: str, directory: Path, max_file_bytes: int, session=None) -> dict:
    from ddgs import DDGS
    try:
        results = DDGS(timeout=10).images(query, max_results=MAX_CANDIDATES, safesearch="moderate")
    except Exception as error:
        raise ImageUnavailable(f"Image search failed: {error}") from error
    errors, started = [], time.monotonic()
    for result in results or []:
        if time.monotonic() - started > SEARCH_SECONDS:
            errors.append("ran out of time")
            break
        image_url = str(result.get("image") or result.get("thumbnail") or "").strip()
        if not image_url.startswith(("https://", "http://")):
            continue
        try:
            path, mime = save_image(download(image_url, session=session), directory, max_file_bytes)
        except Exception as error:
            errors.append(str(error))
            continue
        return {"path": str(path), "name": path.name, "mime": mime, "size": path.stat().st_size,
                "title": str(result.get("title") or "Supplementary image").strip(),
                "source_url": str(result.get("url") or result.get("source") or image_url).strip(),
                "image_url": image_url}
    detail = errors[-1] if errors else "the search returned no downloadable images"
    raise ImageUnavailable(f"Image search failed: {detail}")
