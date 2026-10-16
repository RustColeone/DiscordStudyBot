"""Private key-file boundary for the experimental native WeChat reader."""
import json
import os
from pathlib import Path
import stat
import tempfile

from .database_pages import DatabaseSnapshotError


PROFILE = {'digest': 'sha512', 'reserved': 80, 'page_number_endian': 'little'}
MAX_KEY_FILE_BYTES = 64 * 1024


def target_path(value):
    import re
    return isinstance(value, str) and (value in {'contact/contact.db', 'session/session.db'}
            or re.fullmatch(r'message/message_\d+\.db', value) is not None)


def load_keys(path, account_directory):
    """Reject shared, substituted, mismatched or unsupported key files."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > MAX_KEY_FILE_BYTES):
                raise ValueError()
            document = json.loads(source.read(MAX_KEY_FILE_BYTES + 1))
        if document['account_directory'] != account_directory:
            raise ValueError()
        keys = {}
        for salt, entry in document['keys'].items():
            if not target_path(entry['path']) or entry['profile'] != PROFILE:
                raise ValueError()
            key = bytes.fromhex(entry['key'])
            salt_bytes = bytes.fromhex(salt)
            if len(key) != 32 or len(salt_bytes) != 16 or entry['path'] in keys:
                raise ValueError()
            keys[entry['path']] = (salt_bytes, key)
        if not {'contact/contact.db', 'session/session.db', 'message/message_0.db'} <= keys.keys():
            raise ValueError()
        return keys
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise DatabaseSnapshotError('Native keys unavailable: use a private 0600 key file for this account and supported cipher profile') from None


def save_keys(path, account_directory, keys, uid, gid):
    """Atomic private storage; no plaintext database or key logging."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {uid, os.getuid()}:
        raise ValueError('The private key directory is not owned by the account')
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.keys-')
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, 'w') as destination:
            json.dump({'account_directory': account_directory, 'keys': keys}, destination)
        os.replace(temporary, path)
        os.chown(path.parent, uid, gid)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
