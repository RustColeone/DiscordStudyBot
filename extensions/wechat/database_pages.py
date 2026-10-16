"""Authenticated, read-only SQLCipher-4 snapshots, including committed WAL pages.

Sources: zetetic.net/sqlcipher/design/ and sqlite.org/fileformat2.html#walformat.
Plaintext snapshots exist only in memory. The live files are never opened writable.
"""
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import struct
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


PAGE_SIZE = 4096
RESERVED = 80
MAX_DATABASE_BYTES = 256 * 1024 * 1024
_CAPTURE_LOCK = threading.Lock()


class DatabaseSnapshotError(RuntimeError):
    pass


class PageDecoder:
    def __init__(self, key, salt):
        if len(key) != 32 or len(salt) != 16:
            raise DatabaseSnapshotError('Invalid database key specification')
        self.key, self.salt = key, salt
        self.mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, 32)

    def page(self, data, number):
        if len(data) != PAGE_SIZE or number < 1:
            raise DatabaseSnapshotError('Invalid encrypted page size or number')
        offset = 16 if number == 1 else 0
        if number == 1 and data[:16] != self.salt:
            raise DatabaseSnapshotError('The database salt changed; refresh the account keys')
        expected = hmac.new(self.mac_key, data[offset:-64] + number.to_bytes(4,'little'), 'sha512').digest()
        if not hmac.compare_digest(expected, data[-64:]):
            raise DatabaseSnapshotError('Database page authentication failed')
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(data[-80:-64])).decryptor()
        plain = decryptor.update(data[offset:-80]) + decryptor.finalize()
        return (b'SQLite format 3\0' if number == 1 else b'') + plain + bytes(RESERVED)


def wal_checksum(data, state=(0,0), endian='<'):
    if len(data) % 8:
        raise DatabaseSnapshotError('Invalid WAL checksum input')
    a, b = state
    for left, right in struct.iter_unpack(endian+'II',data):
        a = (a + left + b) & 0xffffffff
        b = (b + right + a) & 0xffffffff
    return a, b


def committed_wal_pages(wal):
    if not wal:
        return {}, None
    if len(wal) < 32:
        raise DatabaseSnapshotError('The WAL header is incomplete')
    magic, version, size, _, salt1, salt2, check1, check2 = struct.unpack('>8I',wal[:32])
    if magic not in {0x377f0682,0x377f0683} or version != 3007000 or size != PAGE_SIZE:
        raise DatabaseSnapshotError('Unsupported WAL format')
    endian = '<' if magic == 0x377f0682 else '>'
    state = wal_checksum(wal[:24],endian=endian)
    if state != (check1,check2):
        raise DatabaseSnapshotError('WAL header checksum failed')
    pending, committed, committed_size = {}, {}, None
    for offset in range(32,len(wal)-23-PAGE_SIZE,24+PAGE_SIZE):
        frame = wal[offset:offset+24+PAGE_SIZE]
        number, commit_size, s1, s2, c1, c2 = struct.unpack('>6I',frame[:24])
        # Old frames may remain after a WAL reset; never read past that boundary.
        if (s1,s2) != (salt1,salt2):
            break
        calculated = wal_checksum(frame[:8]+frame[24:],state,endian)
        if calculated != (c1,c2) or number == 0:
            break
        state = calculated
        pending[number] = frame[24:]
        if commit_size:
            if commit_size * PAGE_SIZE > MAX_DATABASE_BYTES:
                raise DatabaseSnapshotError('The WAL snapshot exceeds the configured size bound')
            committed.update(pending)
            committed = {number:data for number,data in committed.items() if number <= commit_size}
            pending.clear()
            committed_size = commit_size
    return committed, committed_size


def decrypt_snapshot(database, wal, key):
    if not database or len(database) % PAGE_SIZE or len(database) > MAX_DATABASE_BYTES:
        raise DatabaseSnapshotError('Unsupported database size')
    decoder = PageDecoder(key,database[:16])
    overrides, committed_size = committed_wal_pages(wal)
    count = committed_size or len(database)//PAGE_SIZE
    pages = []
    for number in range(1,count+1):
        source = overrides.get(number, database[(number-1)*PAGE_SIZE:number*PAGE_SIZE])
        pages.append(decoder.page(source,number))
    result = bytearray(b''.join(pages))
    if (int.from_bytes(result[16:18],'big') != PAGE_SIZE or result[20] != RESERVED
            or result[21:24] != bytes((64,32,32))):
        raise DatabaseSnapshotError('Unsupported decrypted SQLite header')
    # We already applied the last committed WAL transaction. The private in-memory
    # SQLite connection must not look for a filesystem WAL of its own.
    result[18:20] = b'\x01\x01'
    result[28:32] = count.to_bytes(4,'big')
    return bytes(result)


def file_version(path):
    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def capture_files(path):
    """Brief shared SQLite locks prevent checkpoints and WAL resets while copying.

    No file is created or opened writable. Shared read slots 0 and 1 stop both
    checkpoint backfill and WAL restart; ordinary WAL appends can continue.
    Serializing captures also avoids POSIX locks being released by another
    reader in this process closing a descriptor for the same inode.
    """
    import fcntl
    wal_path, shm_path = Path(str(path)+'-wal'), Path(str(path)+'-shm')
    descriptors = []
    with _CAPTURE_LOCK:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            descriptors.append(descriptor)
            fcntl.lockf(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB, 510, 1073741826, os.SEEK_SET)
            wal_present = wal_path.exists()
            if wal_present:
                shm = os.open(shm_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append(shm)
                for slot in (123,124):
                    fcntl.lockf(shm, fcntl.LOCK_SH | fcntl.LOCK_NB, 1, slot, os.SEEK_SET)
                if os.fstat(shm).st_ino != shm_path.stat().st_ino:
                    raise DatabaseSnapshotError('The native WAL index changed during capture')
            before = file_version(path), file_version(wal_path)
            if (before[0] is None or os.fstat(descriptor).st_ino != before[0][0]
                    or (bool(before[1]) != wal_present)):
                raise DatabaseSnapshotError('The native database changed during capture')
            if any(version and version[1] > MAX_DATABASE_BYTES for version in before):
                raise DatabaseSnapshotError('The account database exceeds the snapshot size bound')
            data = os.pread(descriptor, before[0][1], 0)
            wal = b''
            if before[1]:
                wal_descriptor = os.open(wal_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append(wal_descriptor)
                if os.fstat(wal_descriptor).st_ino != before[1][0]:
                    raise DatabaseSnapshotError('The native WAL changed during capture')
                wal = os.pread(wal_descriptor, before[1][1], 0)
            after = file_version(path), file_version(wal_path)
            if before != after or len(data) != before[0][1] or (before[1] and len(wal) != before[1][1]):
                raise DatabaseSnapshotError('The native database changed during capture')
            return data, wal, after
        except OSError:
            raise DatabaseSnapshotError('The native database snapshot is unavailable or busy') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


class EncryptedDatabase:
    def __init__(self,path,key):
        self.path, self.key = Path(path), key
        self.connection, self.version = None, None

    def refresh(self):
        for _ in range(3):
            try:
                data, wal, after = capture_files(self.path)
            except DatabaseSnapshotError:
                continue
            version = (after,hashlib.sha256(data).digest(),hashlib.sha256(wal).digest())
            if version == self.version and self.connection is not None:
                return self.connection
            plain = decrypt_snapshot(data,wal,self.key)
            connection = sqlite3.connect(':memory:')
            try:
                connection.deserialize(plain)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
            except Exception:
                connection.close()
                raise DatabaseSnapshotError('The decrypted database schema is not readable') from None
            self.close()
            self.connection, self.version = connection, version
            return connection
        raise DatabaseSnapshotError('The account database changed during snapshot capture')

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
