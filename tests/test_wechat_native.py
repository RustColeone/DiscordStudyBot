import ctypes
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.wechat_native import NativeSchemaError, WeChatNativeReader, decode_field, mentions_account, quoted_text, zstd_library
from services.wechat_conversations import WeChatConversationStore
from scripts.wechat_database_keys import authenticates, context_keys


def reader_fixture():
    reader = WeChatNativeReader.__new__(WeChatNativeReader)
    reader.root = Path('/nonexistent-studybot-native-fixture')
    reader.username, reader.account_id = 'wxid_self', 'wxid_self_account'
    reader.bindings = {}
    connections = {name: sqlite3.connect(':memory:') for name in ['contact/contact.db','session/session.db','message/message_0.db']}
    for connection in connections.values():connection.row_factory = sqlite3.Row
    contact = connections['contact/contact.db']
    contact.execute('CREATE TABLE contact(username,remark,nick_name)')
    contact.executemany('INSERT INTO contact VALUES (?,?,?)', [('wxid_self','','Me'),('sender','','Friend'),('room@chatroom','','Group')])
    session = connections['session/session.db']
    session.execute('CREATE TABLE SessionTable(username,unread_count,summary,last_timestamp,is_hidden,sort_timestamp)')
    session.execute("INSERT INTO SessionTable VALUES ('room@chatroom',1,'preview',100,0,100)")
    messages = connections['message/message_0.db']
    import hashlib
    table = 'Msg_'+hashlib.md5(b'room@chatroom').hexdigest()
    messages.execute('CREATE TABLE Name2Id(user_name)')
    messages.executemany('INSERT INTO Name2Id VALUES (?)', [('wxid_self',),('sender',)])
    messages.execute(f'CREATE TABLE "{table}"(local_id,local_type,real_sender_id,create_time,sort_seq,message_content,source,WCDB_CT_source,WCDB_CT_message_content)')
    messages.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?,?)', [
        (1,1,2,100,1,'sender:\n@Me hello','<msgsource><atuserlist>wxid_self</atuserlist></msgsource>',0,0),
        (2,1,1,101,2,'reply','<msgsource/>',0,0),
        (3,1,2,102,3,'@Me spoof','<msgsource/>',0,0)])
    reader.connection = lambda relative, fresh=False: connections[relative]
    reader.databases = {name:None for name in connections}
    return reader, connections, table


class NativeReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader,self.connections,self.table = reader_fixture()
        for connection in self.connections.values():self.addCleanup(connection.close)

    def test_ids_directions_and_explicit_native_mentions(self):
        items = self.reader.read_messages('Group')
        self.assertEqual([m['outgoing'] for m in items], [False,True,False])
        self.assertEqual([m['mentions_me'] for m in items], [True,False,False])
        self.assertEqual(items[0]['text'],'@Me hello')
        self.assertEqual(items[0]['sender_name'],'Friend')
        self.assertTrue(all(m['id'].startswith('native:message/message_0.db:') for m in items))
        before = items[0]['id']
        self.connections['message/message_0.db'].execute(f'UPDATE "{self.table}" SET sort_seq=99 WHERE local_id=1')
        self.assertEqual(self.reader.read_messages('Group')[-1]['id'],before)

    def test_pinned_legacy_chat_survives_rename_without_transferring_whitelist(self):
        self.reader.bind('name:Group','room@chatroom')
        contact = self.connections['contact/contact.db']
        contact.execute("UPDATE contact SET nick_name='Renamed' WHERE username='room@chatroom'")
        contact.execute("INSERT INTO contact VALUES ('other@chatroom','','Group')")
        self.connections['session/session.db'].execute("INSERT INTO SessionTable VALUES ('other@chatroom',0,'',101,0,101)")
        self.assertEqual(self.reader.resolve('name:Group')['native_id'],'room@chatroom')
        self.assertEqual(self.reader.resolve('Renamed')['id'],'name:Group')
        with self.assertRaises(NativeSchemaError):self.reader.bind('name:Group','other@chatroom')

    def test_ambiguous_labels_and_missing_schema_fail_closed(self):
        self.connections['contact/contact.db'].execute("INSERT INTO contact VALUES ('other@chatroom','','Group')")
        self.connections['session/session.db'].execute("INSERT INTO SessionTable VALUES ('other@chatroom',0,'',101,0,101)")
        with self.assertRaises(NativeSchemaError):self.reader.resolve('Group')
        self.assertEqual(self.reader.resolve('native:room@chatroom')['native_id'],'room@chatroom')
        self.connections['message/message_0.db'].execute('DROP TABLE Name2Id')
        with self.assertRaises(NativeSchemaError):self.reader.read_messages('native:room@chatroom')

    def test_unreadable_message_metadata_is_unsupported_activity(self):
        self.connections['message/message_0.db'].execute(f'UPDATE "{self.table}" SET source=? WHERE local_id=1',(b'bad compressed bytes',))
        self.connections['message/message_0.db'].execute(f'UPDATE "{self.table}" SET WCDB_CT_source=4 WHERE local_id=1')
        item = self.reader.read_messages('Group')[0]
        self.assertEqual(item['kind'],'unsupported')
        self.assertIsNone(item['mentions_me'])

    def test_real_zstd_compression_and_bounded_decode(self):
        zstd = zstd_library()
        zstd.ZSTD_compress.argtypes = [ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
        zstd.ZSTD_compress.restype = ctypes.c_size_t
        source = '<msgsource><atuserlist>wxid_self</atuserlist></msgsource>'.encode()
        buffer = ctypes.create_string_buffer(1024)
        size = zstd.ZSTD_compress(buffer,1024,source,len(source),1)
        text = decode_field(buffer.raw[:size],4)
        self.assertTrue(mentions_account(text,'wxid_self'))
        self.assertFalse(mentions_account(text,'wxid_other'))
        with self.assertRaises(NativeSchemaError):decode_field(b'\x28\xb5\x2f\xfdcorrupt',4)
        with self.assertRaises(NativeSchemaError):mentions_account('<!DOCTYPE x><msgsource/>','wxid_self')

    def test_new_message_shard_pauses_reads_but_resource_database_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            self.reader.root = Path(directory)
            folder = self.reader.root/'message';folder.mkdir()
            (folder/'message_resource.db').touch()
            self.assertEqual(len(self.reader.read_messages('Group')),3)
            (folder/'message_1.db').touch()
            with self.assertRaises(NativeSchemaError):self.reader.read_messages('Group')

    def test_quoted_text_is_text_with_an_exact_native_reference(self):
        xml='<msg><appmsg><type>57</type><title><![CDATA[an answer]]></title><refermsg><svrid>123456</svrid></refermsg></appmsg></msg>'
        self.connections['message/message_0.db'].execute(f'UPDATE "{self.table}" SET local_type=?,message_content=? WHERE local_id=2',
                                                       ((57<<32)|49,xml))
        item=self.reader.read_messages('Group')[1]
        self.assertEqual((item['kind'],item['text'],item['reply_to_server_id']),('text','an answer','123456'))
        with self.assertRaises(NativeSchemaError):quoted_text('<msg><appmsg><type>57</type></appmsg></msg>')
