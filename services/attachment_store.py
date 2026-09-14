from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlparse
import zipfile
from xml.etree import ElementTree

import requests


CONVERSATION_CACHE_BYTES = 100 * 1024 * 1024
CACHE_TTL_SECONDS = 2 * 60 * 60
MAX_EXTRACTED_CHARS = 120_000
MAX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
CACHE_ROOT = Path(os.getenv("ATTACHMENT_CACHE_DIR", "temp/attachment_cache"))
DOWNLOAD_HEADERS = {"User-Agent": "DiscordStudyBot/1.0 (+attachment reader)"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
    "text/yaml",
}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".conf", ".cpp", ".css", ".csv", ".go", ".h", ".hpp",
    ".html", ".ini", ".java", ".js", ".json", ".log", ".md", ".py", ".rb",
    ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}


class AttachmentReadError(RuntimeError):
    pass


def attachment_metadata(attachment, message_id=None, author_id=None, created_at=None) -> dict | None:
    url = getattr(attachment, "url", None)
    if not url or not str(url).startswith(("https://", "http://")):
        return None
    filename = getattr(attachment, "filename", None) or Path(urlparse(str(url)).path).name
    if not filename:
        filename = "attachment"
    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].lower()
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "message_id": str(message_id) if message_id is not None else None,
        "attachment_id": str(getattr(attachment, "id", "")) or None,
        "author_id": str(author_id) if author_id is not None else None,
        "filename": filename,
        "url": str(url),
        "content_type": content_type,
        "size": getattr(attachment, "size", None),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


def attachment_inventory(records: list[dict]) -> str:
    if not records:
        return ""
    lines = []
    for record in records:
        size = record.get("size")
        size_text = f", {size} bytes" if size is not None else ""
        lines.append(
            f"- [attachment #{record['id']}] {record['filename']} "
            f"({record.get('content_type') or 'unknown'}{size_text})"
        )
    return "Conversation attachment history (newest first):\n" + "\n".join(lines)


def read_attachment(record: dict) -> dict:
    cached_path = cache_attachment(record)
    content_type = (record.get("content_type") or "application/octet-stream").lower()
    suffix = Path(record["filename"]).suffix.lower()
    result = {
        "id": record["id"],
        "filename": record["filename"],
        "content_type": content_type,
        "size": cached_path.stat().st_size,
        "cached_path": str(cached_path),
        "text": "",
        "image": None,
    }
    if content_type in IMAGE_TYPES:
        result["image"] = {
            "url": record["url"],
            "path": str(cached_path),
            "filename": record["filename"],
            "content_type": content_type,
            "size": result["size"],
        }
    elif content_type == "application/pdf" or suffix == ".pdf":
        result["text"] = _extract_pdf(cached_path)
    elif (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        result["text"] = _extract_docx(cached_path)
    elif content_type.startswith("text/") or content_type in TEXT_TYPES or suffix in TEXT_EXTENSIONS:
        result["text"] = _extract_text(cached_path)
    else:
        result["text"] = (
            "The attachment was cached, but this file type has no safe content extractor configured."
        )
    return result


def cache_attachment(record: dict) -> Path:
    declared_size = record.get("size")
    if declared_size is not None and int(declared_size) > CONVERSATION_CACHE_BYTES:
        raise AttachmentReadError("Attachment exceeds the 100 MiB conversation cache limit")
    cleanup_expired_cache()
    conversation_dir = _conversation_dir(record["channel_id"])
    conversation_dir.mkdir(parents=True, exist_ok=True)
    cached_path = _cache_path(conversation_dir, record)
    if cached_path.is_file():
        cached_path.touch()
        return cached_path

    response = requests.get(
        record["url"],
        stream=True,
        timeout=(10, 60),
        headers=DOWNLOAD_HEADERS,
    )
    try:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > CONVERSATION_CACHE_BYTES:
            raise AttachmentReadError("Attachment exceeds the 100 MiB conversation cache limit")
        expected_size = int(content_length) if content_length else int(declared_size or 0)
        _evict_until_available(conversation_dir, expected_size)
        downloaded = 0
        with tempfile.NamedTemporaryFile(dir=conversation_dir, delete=False) as output:
            temporary_path = Path(output.name)
            try:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > CONVERSATION_CACHE_BYTES:
                        raise AttachmentReadError(
                            "Attachment exceeds the 100 MiB conversation cache limit"
                        )
                    output.write(chunk)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        if downloaded == 0:
            temporary_path.unlink(missing_ok=True)
            raise AttachmentReadError("Downloaded attachment is empty")
        try:
            _evict_until_available(conversation_dir, downloaded, exclude=temporary_path)
            temporary_path.replace(cached_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return cached_path
    finally:
        response.close()


def cleanup_expired_cache(now: float | None = None) -> int:
    if not CACHE_ROOT.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - CACHE_TTL_SECONDS
    deleted = 0
    for path in CACHE_ROOT.glob("*/*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted += 1
    for directory in CACHE_ROOT.iterdir():
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return deleted


def clear_conversation_cache(channel_id: str) -> None:
    directory = _conversation_dir(channel_id)
    if not directory.exists():
        return
    for path in directory.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)
    directory.rmdir()


def _conversation_dir(channel_id: str) -> Path:
    digest = hashlib.sha256(str(channel_id).encode("utf-8")).hexdigest()[:24]
    return CACHE_ROOT / digest


def _cache_path(directory: Path, record: dict) -> Path:
    suffix = Path(record["filename"]).suffix.lower()
    safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""
    digest = hashlib.sha256(record["url"].encode("utf-8")).hexdigest()[:16]
    return directory / f"{record['id']}-{digest}{safe_suffix}"


def _evict_until_available(directory: Path, incoming_bytes: int, exclude: Path | None = None) -> None:
    files = [
        path for path in directory.iterdir()
        if path.is_file() and path != exclude
    ]
    used = sum(path.stat().st_size for path in files)
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if used + incoming_bytes <= CONVERSATION_CACHE_BYTES:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        used -= size
    if used + incoming_bytes > CONVERSATION_CACHE_BYTES:
        raise AttachmentReadError("Not enough room in the 100 MiB conversation cache")


def _extract_text(path: Path) -> str:
    raw = path.read_bytes()[: MAX_EXTRACTED_CHARS * 4]
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)[:MAX_EXTRACTED_CHARS]
        except UnicodeDecodeError:
            continue
    raise AttachmentReadError("The text attachment could not be decoded")


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise AttachmentReadError("PDF support requires the pypdf package") from error
    reader = PdfReader(str(path))
    parts = []
    length = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
        length += len(text)
        if length >= MAX_EXTRACTED_CHARS:
            break
    extracted = "\n\n".join(parts).strip()[:MAX_EXTRACTED_CHARS]
    return extracted or "The PDF did not contain extractable text."


def _extract_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            document_info = archive.getinfo("word/document.xml")
            if document_info.file_size > MAX_DOCUMENT_XML_BYTES:
                raise AttachmentReadError("The DOCX document content is too large to extract safely")
            document = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as error:
        raise AttachmentReadError("The DOCX attachment is invalid or unreadable") from error
    root = ElementTree.fromstring(document)
    text = "\n".join(node.text for node in root.iter() if node.tag.endswith("}t") and node.text)
    return text[:MAX_EXTRACTED_CHARS] or "The DOCX did not contain extractable text."