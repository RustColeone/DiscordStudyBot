import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.wechat_database_keys import authenticates, binary_key_masks, context_keys
from services.wechat_database_keys import PROFILE, load_keys, save_keys, target_path
from services.wechat_database_pages import DatabaseSnapshotError
from test_wechat_database_pages import KEY, SALT, encrypted_fixture


class NativeKeyTests(unittest.TestCase):
    def test_page_authentication_accepts_only_correct_key(self):
        page = encrypted_fixture()[:4096]
        self.assertTrue(authenticates(page, KEY))
        self.assertFalse(authenticates(page, bytes(32)))
        self.assertFalse(authenticates(page, b'short'))

    def test_public_context_layouts_require_real_page_authentication(self):
        import struct
        for displacement in (48, 56):
            memory = bytearray(2048)
            struct.pack_into('<6Q', memory, 128+displacement, 800, 850, 900, 950, 1000, 1050)
            memory[800:816] = SALT
            struct.pack_into('<Q', memory, 1008, 1200)
            memory[1200:1232] = KEY
            def read(address, size):
                return bytes(memory[address:address+size]) if address >= 128 else b''
            self.assertEqual(list(context_keys(read, 128, {SALT: ('contact/contact.db', encrypted_fixture()[:4096])})), [(SALT, KEY)])
            memory[1200] ^= 1
            self.assertEqual(list(context_keys(read, 128, {SALT: ('contact/contact.db', encrypted_fixture()[:4096])})), [])

    def test_masked_context_keys_still_require_page_authentication(self):
        import struct
        mask = bytes(reversed(range(32)))
        memory = bytearray(2048)
        struct.pack_into('<6Q',memory,184,800,850,900,950,1000,1050)
        memory[800:816] = SALT
        struct.pack_into('<Q',memory,1008,1200)
        memory[1200:1232] = bytes(a^b for a,b in zip(KEY,mask))
        def read(address,size):return bytes(memory[address:address+size]) if address >= 128 else b''
        pages = {SALT:('contact/contact.db',encrypted_fixture()[:4096])}
        self.assertEqual(list(context_keys(read,128,pages)),[])
        self.assertEqual(list(context_keys(read,128,pages,[mask])),[(SALT,KEY)])

    def test_mask_discovery_follows_arm64_elf_code_without_fixed_addresses(self):
        import struct
        binary = bytearray(0x3000)
        binary[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H',binary,18,183)
        struct.pack_into('<Q',binary,32,64)
        struct.pack_into('<HH',binary,54,56,2)
        struct.pack_into('<II6Q',binary,64,1,4,0,0,0,0x2000,0x2000,4096)
        struct.pack_into('<II6Q',binary,120,1,5,0x2000,0x4000,0x4000,0x100,0x100,4096)
        # ADRP x12,-3 pages ; LDR q4,[x12,#0x100]
        # ADRP x10,-3 pages ; LDR q5,[x10,#0x200] ; STP q4,q5,[x29,#-32]
        for i,instruction in enumerate([0xb0ffffec,0x3dc04184,0xb0ffffea,0x3dc08145,0xad3f17a4]):
            struct.pack_into('<I',binary,0x2000+i*4,instruction)
        mask = bytes(range(32))
        binary[0x1100:0x1110] = mask[:16];binary[0x1200:0x1210] = mask[16:]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'client'
            path.write_bytes(binary)
            self.assertEqual(binary_key_masks(path),[mask])

    def test_private_storage_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'private'/'keys.json'
            entries = {bytes([i]*16).hex(): {'path': relative, 'key': KEY.hex(), 'profile': PROFILE}
                       for i, relative in enumerate(['contact/contact.db', 'session/session.db', 'message/message_0.db'])}
            save_keys(path, 'wxid_test', entries, os.getuid(), os.getgid())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(load_keys(path, 'wxid_test')['contact/contact.db'][1], KEY)
            for account, mode in [('wxid_other', 0o600), ('wxid_test', 0o644)]:
                path.chmod(mode)
                with self.assertRaises(DatabaseSnapshotError):load_keys(path, account)
            path.chmod(0o600)
            link = Path(directory)/'link'
            link.symlink_to(path)
            with self.assertRaises(DatabaseSnapshotError):load_keys(link, 'wxid_test')
            entries[next(iter(entries))]['profile'] = {**PROFILE, 'reserved': 48}
            path.write_text(json.dumps({'account_directory': 'wxid_test', 'keys': entries}))
            with self.assertRaises(DatabaseSnapshotError):load_keys(path, 'wxid_test')

    def test_paths_are_limited_to_approved_databases(self):
        for value in ['contact/contact.db', 'session/session.db', 'message/message_0.db', 'message/message_21.db']:
            self.assertTrue(target_path(value))
        for value in ['../contact.db', '/message/message_0.db', 'contact/other.db', 'message/message_0.db-wal', [], None]:
            self.assertFalse(target_path(value))
