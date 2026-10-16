from pathlib import Path
import hashlib
import hmac
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.wechat_database_pages import (PageDecoder, DatabaseSnapshotError, EncryptedDatabase,
    capture_files, committed_wal_pages, decrypt_snapshot, wal_checksum)


KEY = bytes(range(32))
SALT = bytes(range(16))


def sqlite_fixture(value='hello'):
    """Small valid SQLite database with SQLCipher's 80 reserved bytes per page."""
    def record(values):
        serial, body = [], b''
        for value in values:
            if isinstance(value,int):
                serial.append(1);body += bytes([value])
            else:
                raw=value.encode();serial.append(13+2*len(raw));body += raw
        return bytes([len(serial)+1,*serial])+body
    def page(payload, offset=0):
        result=bytearray(4096)
        cell=bytes([len(payload),1])+payload
        start=4016-len(cell)
        result[offset:offset+8]=struct.pack('>BHHHB',13,0,1,start,0)
        result[offset+8:offset+10]=start.to_bytes(2,'big')
        result[start:start+len(cell)]=cell
        return result
    schema=record(['table','records','records',2,'CREATE TABLE records(value TEXT)'])
    first=page(schema,100)
    first[:16]=b'SQLite format 3\0'
    first[16:24]=bytes((16,0,1,1,80,64,32,32))
    for offset,field_value in [(24,1),(28,2),(40,1),(44,4),(56,1),(92,1),(96,3040000)]:
        first[offset:offset+4]=field_value.to_bytes(4,'big')
    return bytes(first)+bytes(page(record([value])))


def encrypt(plain,number=1):
    offset=16 if number==1 else 0
    iv=bytes([number%256])*16
    encryptor=Cipher(algorithms.AES(KEY),modes.CBC(iv)).encryptor()
    cipher=encryptor.update(plain[offset:-80])+encryptor.finalize()
    mac_key=hashlib.pbkdf2_hmac('sha512',KEY,bytes(v^0x3a for v in SALT),2,32)
    mac=hmac.new(mac_key,cipher+iv+number.to_bytes(4,'little'),'sha512').digest()
    return (SALT if number==1 else b'')+cipher+iv+mac


def encrypted_fixture(value='hello'):
    plain=sqlite_fixture(value)
    return b''.join(encrypt(plain[i:i+4096],i//4096+1) for i in range(0,len(plain),4096))


def wal_fixture(frames):
    header=struct.pack('>6I',0x377f0682,3007000,4096,0,42,43)
    state=wal_checksum(header)
    result=header+struct.pack('>2I',*state)
    for number,size,data in frames:
        prefix=struct.pack('>2I',number,size)
        state=wal_checksum(prefix+data,state)
        result+=prefix+struct.pack('>4I',42,43,*state)+data
    return result


class NativePageTests(unittest.TestCase):
    def test_authenticated_snapshot_queries_real_sqlite_in_memory(self):
        plain=decrypt_snapshot(encrypted_fixture(),b'',KEY)
        connection=sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.deserialize(plain)
        self.assertEqual(connection.execute('SELECT value FROM records').fetchone()[0],'hello')

    def test_wrong_key_tampered_ciphertext_and_wrong_page_number_fail_closed(self):
        encrypted=encrypted_fixture()
        for decoder,data,number in [(PageDecoder(bytes(32),SALT),encrypted[:4096],1),
                                    (PageDecoder(KEY,SALT),encrypted[:20]+b'X'+encrypted[21:4096],1),
                                    (PageDecoder(KEY,SALT),encrypted[4096:],3)]:
            with self.assertRaises(DatabaseSnapshotError):decoder.page(data,number)

    def test_wal_uses_committed_update_and_ignores_uncommitted_or_partial_tail(self):
        initial=encrypted_fixture('old')
        committed=encrypt(sqlite_fixture('committed')[4096:],2)
        uncommitted=encrypt(sqlite_fixture('uncommitted')[4096:],2)
        wal=wal_fixture([(2,2,committed),(2,0,uncommitted)])+b'partial'
        plain=decrypt_snapshot(initial,wal,KEY)
        connection=sqlite3.connect(':memory:');self.addCleanup(connection.close)
        connection.deserialize(plain)
        self.assertEqual(connection.execute('SELECT value FROM records').fetchone()[0],'committed')

    def test_wal_reset_old_frames_and_invalid_checksums_do_not_become_updates(self):
        wal=bytearray(wal_fixture([(2,2,encrypt(sqlite_fixture()[4096:],2))]))
        wal[40:44]=(99).to_bytes(4,'big')
        self.assertEqual(committed_wal_pages(bytes(wal)),({},None))
        wal=bytearray(wal_fixture([(2,2,encrypt(sqlite_fixture()[4096:],2))]))
        wal[-100]^=1
        self.assertEqual(committed_wal_pages(bytes(wal)),({},None))
        wal[0]^=1
        with self.assertRaises(DatabaseSnapshotError):committed_wal_pages(bytes(wal))

    def test_database_cache_reads_changes_without_writing_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'native.db'
            path.write_bytes(encrypted_fixture('first'))
            reader=EncryptedDatabase(path,KEY);self.addCleanup(reader.close)
            original=path.read_bytes();before=path.stat().st_mtime_ns
            first=reader.refresh()
            self.assertEqual(first.execute('SELECT value FROM records').fetchone()[0],'first')
            self.assertIs(first,reader.refresh())
            with self.assertRaises(sqlite3.OperationalError):first.execute("DELETE FROM records")
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(path.stat().st_mtime_ns,before)
            path.write_bytes(encrypted_fixture('second'))
            self.assertEqual(reader.refresh().execute('SELECT value FROM records').fetchone()[0],'second')

    def test_wal_parser_accepts_stock_sqlite_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'plain.db'
            connection=sqlite3.connect(path);self.addCleanup(connection.close)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA wal_autocheckpoint=0')
            connection.execute('CREATE TABLE sample(value)')
            connection.execute("INSERT INTO sample VALUES ('real SQLite WAL')")
            connection.commit()
            pages,size=committed_wal_pages(Path(str(path)+'-wal').read_bytes())
            self.assertEqual(size,connection.execute('PRAGMA page_count').fetchone()[0])
            self.assertIn(2,pages)

    def test_capture_locks_block_real_sqlite_checkpoint_only_during_copy(self):
        # A separate SQLite process exercises real VFS locks, rather than mocks
        # of our lock calls. Its temporary DB contains no production messages.
        program = '''
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE sample(value)')
connection.execute("INSERT INTO sample VALUES ('committed')")
connection.commit()
print('ready', flush=True)
for line in sys.stdin:
    print(connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'native.db'
            process = subprocess.Popen([sys.executable, '-c', program, str(path)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                def checkpoint():
                    process.stdin.write('checkpoint\n');process.stdin.flush()
                    return process.stdout.readline().strip()
                import os
                original = os.pread
                checked = []
                def read(*args):
                    if not checked:
                        checked.append(checkpoint())
                    return original(*args)
                with patch('services.wechat_database_pages.os.pread', side_effect=read):
                    data, wal, _ = capture_files(path)
                self.assertEqual(checked, ['1'])  # SQLITE_BUSY while copying
                self.assertTrue(data.startswith(b'SQLite format 3'))
                self.assertTrue(committed_wal_pages(wal)[0])
                self.assertEqual(checkpoint(), '0')  # released promptly
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
                process.stdout.close()

    def test_changed_invalid_source_does_not_return_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'native.db'
            path.write_bytes(encrypted_fixture())
            reader = EncryptedDatabase(path, KEY);self.addCleanup(reader.close)
            reader.refresh()
            invalid = bytearray(path.read_bytes());invalid[100] ^= 1
            path.write_bytes(invalid)
            with self.assertRaises(DatabaseSnapshotError):reader.refresh()
            # A WAL with no lockable index must not be treated as a safe live read.
            Path(str(path)+'-wal').write_bytes(b'')
            with self.assertRaises(DatabaseSnapshotError):reader.refresh()
