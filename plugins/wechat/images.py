"""Decrypting WeChat 4 image files (`.dat`, "V2" format) with the account's image key.

Layout: a 15-byte header (`07 08 56 32 08 07`, AES length, XOR length, one byte), then the first
`aes_length` bytes AES-128-ECB encrypted with PKCS#7 padding, then plain bytes, then the last
`xor_length` bytes XORed with one key byte. Version 1 files (`07 08 56 31 08 07`) use a fixed AES key.
Files from before version 1 are plain XOR.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
from pathlib import Path

V1, V2 = b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"
V1_KEY = b"cfcd208495d565ef"
MAGIC = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png"), b"GIF8": ("gif", "image/gif"),
         b"RIFF": ("webp", "image/webp")}
MD5 = re.compile(rb"([0-9a-f]{32})")


class ImageUnavailable(RuntimeError):
    pass


def image_type(data: bytes) -> tuple[str, str] | None:
    return next((value for magic, value in MAGIC.items() if data.startswith(magic)), None)


def load_image_key(path) -> tuple[bytes, int] | None:
    """The private 0600 key file written by image_key.py, or None when images are not set up."""
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ImageUnavailable("The WeChat image key file must be a private 0600 file owned by the bot user")
    value = json.loads(path.read_text())
    return bytes.fromhex(value["aes_key"]), int(value["xor_key"])


def decrypt(data: bytes, aes_key: bytes | None, xor_key: int) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if data[:6] in (V1, V2):
        if len(data) < 15:
            raise ImageUnavailable("The image file is truncated")
        aes_length, xor_length = struct.unpack_from("<II", data, 6)
        key = V1_KEY if data[:6] == V1 else aes_key
        if key is None:
            raise ImageUnavailable("The WeChat image key is not set up")
        encrypted = aes_length - aes_length % 16 + 16
        body = data[15:]
        if encrypted + xor_length > len(body):
            raise ImageUnavailable("The image file is truncated")
        decryptor = Cipher(algorithms.AES(key[:16]), modes.ECB()).decryptor()
        head = decryptor.update(body[:encrypted]) + decryptor.finalize()
        head = head[:-head[-1]] if 0 < head[-1] <= 16 else head
        middle = body[encrypted:len(body) - xor_length]
        tail = bytes(byte ^ xor_key for byte in body[len(body) - xor_length:]) if xor_length else b""
        return head + middle + tail
    # Old format: the whole file XORed with one byte, recognised from the image magic.
    for magic in MAGIC:
        key = data[0] ^ magic[0]
        if all(data[index] ^ key == magic[index] for index in range(len(magic))):
            return bytes(byte ^ key for byte in data)
    raise ImageUnavailable("Unrecognised WeChat image file")


def image_md5(packed_info: bytes | None) -> str | None:
    """The file name hash WeChat stores in a message's packed info (a protobuf string field)."""
    match = MD5.search(packed_info or b"")
    return match.group(1).decode() if match else None


def find_image(account_dir: Path, chat_id: str, md5: str) -> Path | None:
    """The best local copy: original, then high definition, then the thumbnail every message has."""
    base = Path(account_dir) / "msg" / "attach" / hashlib.md5(chat_id.encode()).hexdigest()
    for suffix in ("", "_h", "_t"):
        found = sorted(base.glob(f"*/Img/{md5}{suffix}.dat"))
        if found:
            return found[-1]
    return None


def extract(source: Path, target_dir: Path, key: tuple[bytes, int] | None, max_bytes: int = 20 * 1024 * 1024) -> Path:
    """Decrypt one image file into `target_dir` once; returns the cached copy on later calls."""
    if source.stat().st_size > max_bytes:
        raise ImageUnavailable("The image is too large")
    target_dir.mkdir(parents=True, exist_ok=True)
    for existing in target_dir.glob(source.stem + ".*"):
        return existing
    aes_key, xor_key = key if key else (None, 0)
    data = decrypt(source.read_bytes(), aes_key, xor_key)
    kind = image_type(data)
    if kind is None:
        raise ImageUnavailable("The decrypted file is not an image (wrong image key?)")
    path = target_dir / f"{source.stem}.{kind[0]}"
    temporary = path.with_suffix(".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return path


def mime_for(path: Path) -> str:
    return next((mime for extension, mime in MAGIC.values() if path.suffix == "." + extension), "")
