"""Bounded, read-only recovery of this account's WeChat image key (for `.dat` images in format V2).

Requires explicit account-owner approval and permission to read the native process, like
key_setup.py. The key is accepted only if it decrypts the account's own image files to real images;
nothing else is saved. The XOR byte is derived from the files themselves.

Run from the repository root as the account owner, once:
    sudo venv/bin/python -m extensions.wechat.image_key --pid <native-wechat-pid> \
        --account-dir /path/to/xwechat_files/wxid_x --output data/core/extensions/wechat/image_key.json
"""

import argparse
import json
import os
import re
import struct
import time
from pathlib import Path

from .images import V2, decrypt, image_type

MAX_SCAN_BYTES = 3 * 1024**3
MAX_REGION_BYTES = 512 * 1024**2
MAX_SCAN_SECONDS = 120
CANDIDATE = re.compile(rb"[0-9a-zA-Z]{16,64}")
HEX32 = re.compile(rb"[0-9a-f]{32}")


def candidates(run: bytes):
    """Keys a run of letters and digits may hold: each 16-character window, or 32 hex digits as bytes."""
    for start in range(0, len(run) - 15):
        yield run[start:start + 16]
    for match in HEX32.finditer(run):
        yield bytes.fromhex(match.group(0).decode())


def samples(account_dir: Path, count: int = 8) -> list[bytes]:
    """Recent V2 thumbnails: small, always present, usually JPEG inside."""
    files = sorted((account_dir / "msg" / "attach").glob("*/*/Img/*_t.dat"), key=lambda p: p.stat().st_mtime)
    result = []
    for path in reversed(files):
        data = path.read_bytes()
        if data[:6] == V2 and len(data) > 64 and struct.unpack_from("<II", data, 6)[1] >= 2:
            result.append(data)
        if len(result) >= count:
            break
    if not result:
        raise ValueError("No V2 image files were found; receive one image in WeChat first")
    return result


def xor_key(files: list[bytes]) -> tuple[int, list[bytes]]:
    """JPEG files end with FF D9, so the XORed tail reveals the key byte. The most common byte wins
    (a few images carry trailing data); it returns the samples that agree with it."""
    votes: dict[int, list[bytes]] = {}
    for data in files:
        if data[-2] ^ data[-1] == 0xFF ^ 0xD9:
            votes.setdefault(data[-1] ^ 0xD9, []).append(data)
    if not votes:
        raise ValueError("Could not derive the XOR key from the image tails")
    key = max(votes, key=lambda value: len(votes[value]))
    return key, votes[key]


def discover(pid: int, account_dir: Path, output: Path) -> None:
    account_dir = account_dir.resolve(strict=True)
    if not account_dir.name.startswith("wxid_"):
        raise ValueError("Choose one account directory (…/xwechat_files/wxid_…)")
    process = Path("/proc") / str(pid)
    if process.joinpath("exe").resolve(strict=True) != Path("/opt/wechat/wechat"):
        raise ValueError("The selected process is not the native WeChat client")
    owner = account_dir.stat()
    uid = next(line.split()[1] for line in process.joinpath("status").read_text().splitlines() if line.startswith("Uid:"))
    if int(uid) != owner.st_uid:
        raise ValueError("The process and account directory have different owners")
    xor, files = xor_key(samples(account_dir))
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    first_block = files[0][15:31]

    def plausible(key: bytes) -> bool:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return image_type(decryptor.update(first_block)) is not None

    tried, scanned, found = set(), 0, None
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    regions = []
    for line in process.joinpath("maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        start, end = (int(value, 16) for value in fields[0].split("-"))
        name = fields[5] if len(fields) > 5 else ""
        if fields[1].startswith("rw") and (not name or name.startswith("[")) and end - start <= MAX_REGION_BYTES:
            regions.append((start, end))
    with process.joinpath("mem").open("rb", buffering=0) as memory:
        for start, end in regions:
            tail = b""
            for offset in range(start, end, 4 * 1024**2):
                if time.monotonic() > deadline or scanned >= MAX_SCAN_BYTES:
                    raise ValueError("The bounded read-only scan limit was reached; no key file was written")
                try:
                    block = os.pread(memory.fileno(), min(4 * 1024**2, end - offset), offset)
                except OSError:
                    break
                scanned += len(block)
                data = tail + block
                for match in CANDIDATE.finditer(data):
                    for key in candidates(match.group(0)):
                        if key in tried:
                            continue
                        tried.add(key)
                        if plausible(key) and all(image_type(decrypt(sample, key, xor)) for sample in files):
                            found = key
                            break
                    if found:
                        break
                if found:
                    break
                tail = data[-64:]
            if found:
                break
    if found is None:
        raise ValueError(f"No image key found after a bounded {scanned // 1024**2} MiB read "
                         f"({len(tried)} candidates); no key file was written")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump({"account": account_dir.name, "aes_key": found.hex(), "xor_key": xor}, handle)
    os.chown(output, owner.st_uid, owner.st_gid)
    print(json.dumps({"verified_images": len(files), "private_key_file": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--account-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        discover(arguments.pid, arguments.account_dir, arguments.output)
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(1)
