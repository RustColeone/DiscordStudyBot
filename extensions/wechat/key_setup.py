"""Bounded, read-only recovery of this account's native WeChat database keys.

Requires explicit account-owner approval and permission to read the native process.
No debugger attach, process writes, injection, raw-memory dumps or key logging.
Only authenticated SQLCipher-4 keys for contact/session/message databases are saved.

Run from the repository root as the account owner, once (copied from scripts/wechat_database_keys.py):
    sudo venv/bin/python -m extensions.wechat.key_setup --pid <native-wechat-pid> \
        --database-root /path/to/wxid_x/db_storage --output data/core/extensions/wechat/keys.json
"""
import argparse
import json
import os
from pathlib import Path
import re
import struct
import time

from .database_keys import PROFILE, save_keys, target_path
from .database_pages import DatabaseSnapshotError, PageDecoder

MAX_SCAN_BYTES = 2 * 1024**3
MAX_REGION_BYTES = 512 * 1024**2
MAX_SCAN_SECONDS = 45
CONTEXT_NEEDLE = struct.pack('<4I', 32, 16, 16, 4096)
HEX_KEY = re.compile(rb"x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")


def binary_key_masks(path):
    """Find ARM64 constant-vector XOR masks in the installed ELF, without fixed addresses.

    Some native Tencent builds mask cipher keys at rest. Their page-cipher routine
    loads two constant vectors and stores that pair before its XOR loop. Deriving
    candidates from code permits ASLR/build changes; page authentication remains
    mandatory. Unsupported architectures simply retain raw-key recovery.
    """
    binary = Path(path).read_bytes()
    if binary[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', binary, 18)[0] != 183:
        return []
    phoff = struct.unpack_from('<Q', binary, 32)[0]
    phsize, count = struct.unpack_from('<HH', binary, 54)
    loads = []
    for index in range(count):
        kind, flags, offset, address, _, size, _, _ = struct.unpack_from('<II6Q', binary, phoff+index*phsize)
        if kind == 1:
            loads.append((offset, address, size, flags))
    def at(address, size):
        for offset, start, length, flags in loads:
            if start <= address and address+size <= start+length and not flags & 2:
                return binary[offset+address-start:offset+address-start+size]
        return b''
    masks = set()
    # STP q4,q5,[x29,#-32], as used by the observed page-cipher XOR routine.
    for offset, address, length, flags in loads:
        if not flags & 1:
            continue
        segment = binary[offset:offset+length]
        location = segment.find(bytes.fromhex('a4173fad'))
        while location >= 0:
            if location % 4 == 0:
                bases, vectors = {}, {}
                for position in range(max(0,location-128),location,4):
                    instruction = struct.unpack_from('<I',segment,position)[0]
                    if instruction & 0x9f000000 == 0x90000000:  # ADRP
                        immediate = ((instruction >> 5 & 0x7ffff)<<2) | (instruction >> 29 & 3)
                        if immediate & (1<<20): immediate -= 1<<21
                        bases[instruction & 31] = ((address+position) & ~4095) + (immediate<<12)
                    elif instruction & 0xffc00000 == 0x3dc00000:  # LDR Q, unsigned offset
                        base = bases.get(instruction >> 5 & 31)
                        if base is not None:
                            vectors[instruction & 31] = at(base+(instruction >> 10 & 0xfff)*16,16)
                if len(vectors.get(4,b'')) == 16 and len(vectors.get(5,b'')) == 16:
                    masks.add(vectors[4]+vectors[5])
                    if len(masks) >= 32:
                        return sorted(masks)
            location = segment.find(bytes.fromhex('a4173fad'),location+4)
    return sorted(masks)


def authenticates(page, key):
    try:
        decoded = PageDecoder(key, page[:16]).page(page, 1)
        return decoded[16:18] == b'\x10\x00' and decoded[20:24] == bytes((80, 64, 32, 32))
    except (DatabaseSnapshotError, ValueError):
        return False


def context_keys(read, address, pages, masks=()):
    """SQLCipher community / Tencent layouts; offsets are hypotheses, MAC is proof.

    Sources: SQLCipher 4.6.1 src/sqlcipher.c; Tencent/sqlcipher src/crypto_impl.c.
    Tencent adds skip_read_hmac and need_kdf_salt before flags. On ARM64 their
    salt pointers begin 56 bytes after key_sz; the community offset is 48.
    """
    for displacement in (48, 56):
        data = read(address + displacement, 48)
        if len(data) != 48:
            continue
        pointers = struct.unpack('<6Q', data)
        salt = read(pointers[0], 16)
        if salt not in pages:
            continue
        for context in pointers[4:6]:
            cipher = read(context, 40)
            if len(cipher) != 40:
                continue
            key_pointer = struct.unpack_from('<Q', cipher, 8)[0]
            key = read(key_pointer, 32)
            if len(key) == 32:
                for candidate in [key, *(bytes(a^b for a,b in zip(key,mask)) for mask in masks)]:
                    if authenticates(pages[salt][1], candidate):
                        yield salt, candidate


def discover(pid, root, output):
    root = root.resolve(strict=True)
    if root.name != 'db_storage' or not root.parent.name.startswith('wxid_'):
        raise ValueError('Choose one account db_storage directory')
    process = Path('/proc') / str(pid)
    if process.joinpath('exe').resolve(strict=True) != Path('/opt/wechat/wechat'):
        raise ValueError('The selected process is not the native WeChat client')
    owner = root.stat().st_uid
    uid = next(line.split()[1] for line in process.joinpath('status').read_text().splitlines() if line.startswith('Uid:'))
    if int(uid) != owner:
        raise ValueError('The process and database directory have different owners')
    pages = {}
    for path in root.rglob('*.db'):
        relative = path.relative_to(root).as_posix()
        if target_path(relative):
            with path.open('rb') as source:
                page = source.read(4096)
            if len(page) == 4096 and not page.startswith(b'SQLite format 3\0'):
                pages[page[:16]] = (relative, page)
    required = {'contact/contact.db', 'session/session.db', 'message/message_0.db'}
    if not required <= {entry[0] for entry in pages.values()}:
        raise ValueError('The required native account databases are unavailable')
    ranges = []
    for line in process.joinpath('maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        start, end = (int(v, 16) for v in fields[0].split('-'))
        ranges.append((start, end, fields[1], fields[5] if len(fields) > 5 else ''))
    verified, scanned, contexts = {}, 0, 0
    masks = binary_key_masks(process.joinpath('exe'))
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    with process.joinpath('mem').open('rb', buffering=0) as memory:
        def read(address, size):
            if not any(a <= address and address + size <= b and p.startswith('r') for a, b, p, _ in ranges):
                return b''
            try:
                return os.pread(memory.fileno(), size, address)
            except OSError:
                return b''
        def retain(salt, key):
            verified[salt.hex()] = {'path': pages[salt][0], 'key': key.hex(), 'profile': PROFILE}
        for start, end, permissions, name in ranges:
            if permissions != 'rw-p' or (name and not name.startswith('[')) or end-start > MAX_REGION_BYTES:
                continue
            tail = b''
            for offset in range(start, end, 2 * 1024**2):
                if time.monotonic() > deadline or scanned >= MAX_SCAN_BYTES:
                    raise ValueError('The bounded read-only recovery limit was reached; no key file was written')
                block = read(offset, min(2 * 1024**2, end-offset))
                scanned += len(block)
                data = tail + block
                location = data.find(CONTEXT_NEEDLE)
                while location >= 0:
                    if contexts >= 256:
                        raise ValueError('The bounded cipher-context limit was reached; no key file was written')
                    contexts += 1
                    for salt, key in context_keys(read, offset-len(tail)+location, pages, masks):
                        retain(salt, key)
                    location = data.find(CONTEXT_NEEDLE, location+16)
                for match in HEX_KEY.finditer(data):
                    salt, key = bytes.fromhex(match[2].decode()), bytes.fromhex(match[1].decode())
                    if salt in pages and authenticates(pages[salt][1], key):
                        retain(salt, key)
                tail = data[-128:]
            if len(verified) == len(pages):
                break
    if len(verified) != len(pages):
        raise ValueError(f'Native reading is not ready: authenticated {len(verified)}/{len(pages)} database keys '
                         f'after a bounded {scanned//1024**2} MiB read ({contexts} cipher contexts); no key file was written')
    save_keys(output, root.parent.name, verified, owner, root.stat().st_gid)
    print(json.dumps({'verified_databases': [entry['path'] for entry in verified.values()],
                      'private_key_file': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--database-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='private key file, e.g. data/core/extensions/wechat/keys.json')
    args = parser.parse_args()
    try:
        discover(args.pid, args.database_root, args.output)
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
