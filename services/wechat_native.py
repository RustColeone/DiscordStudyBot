"""Read-only native WeChat contacts, sessions and messages; no desktop controls."""
import ctypes
import ctypes.util
import hashlib
from functools import lru_cache
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from services.wechat_database_keys import load_keys, target_path
from services.wechat_database_pages import DatabaseSnapshotError, EncryptedDatabase


class NativeSchemaError(DatabaseSnapshotError):
    pass


@lru_cache(maxsize=1)
def zstd_library():
    library = ctypes.util.find_library('zstd')
    if not library:
        raise NativeSchemaError('Native message compression requires libzstd')
    zstd = ctypes.CDLL(library)
    zstd.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    zstd.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
    zstd.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    zstd.ZSTD_decompress.restype = ctypes.c_size_t
    zstd.ZSTD_isError.argtypes = [ctypes.c_size_t]
    zstd.ZSTD_isError.restype = ctypes.c_uint
    return zstd


def decode_field(value, compression=0):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        raise NativeSchemaError('Unsupported native message field')
    if value.startswith(b'\x28\xb5\x2f\xfd'):
        zstd = zstd_library()
        size = zstd.ZSTD_getFrameContentSize(value, len(value))
        if size > 4 * 1024 * 1024:
            raise NativeSchemaError('Native compressed field exceeds the decode bound')
        buffer = ctypes.create_string_buffer(size)
        written = zstd.ZSTD_decompress(buffer, size, value, len(value))
        if zstd.ZSTD_isError(written) or written != size:
            raise NativeSchemaError('Unsupported or corrupt native compressed field')
        value = buffer.raw[:written]
    elif compression not in (None, 0):
        raise NativeSchemaError('Unsupported native compression format')
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        raise NativeSchemaError('Native text is not UTF-8') from None


def mentions_account(source, username):
    if not source:
        return False
    if len(source) > 1024 * 1024 or '<!DOCTYPE' in source.upper() or '<!ENTITY' in source.upper():
        raise NativeSchemaError('Unsupported native mention metadata')
    try:
        document = ET.fromstring(source)
    except ET.ParseError:
        raise NativeSchemaError('Native mention metadata is not readable') from None
    if document.tag != 'msgsource':
        raise NativeSchemaError('Unsupported native mention metadata root')
    # Native user IDs avoid nickname changes and accidental textual @mentions.
    identifiers = [entry.strip() for node in document.iter('atuserlist')
                   for entry in (node.text or '').split(',')]
    return username in identifiers


def quoted_text(content):
    if not content.lstrip().startswith('<'):
        return None
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        raise NativeSchemaError('Unsupported quoted message XML')
    try:
        document = ET.fromstring(content)
    except ET.ParseError:
        return None
    app = document if document.tag=='appmsg' else document.find('.//appmsg')
    if app is None or app.findtext('type') != '57':
        return None
    reference = app.find('refermsg')
    if reference is None or not reference.findtext('svrid'):
        raise NativeSchemaError('Quoted message has no original server ID')
    return app.findtext('title') or '', reference.findtext('svrid')


class WeChatNativeReader:
    def __init__(self, root, key_file):
        self.root = Path(root).resolve(strict=True)
        if self.root.name != 'db_storage' or not self.root.parent.name.startswith('wxid_'):
            raise NativeSchemaError('Select one native account db_storage directory')
        self.account_id = self.root.parent.name
        self.username = self.account_id.rsplit('_', 1)[0]
        keys = load_keys(key_file, self.account_id)
        self.databases = {relative: EncryptedDatabase(self.root/relative, key) for relative, (salt, key) in keys.items()}
        for relative, (salt, key) in keys.items():
            with (self.root/relative).open('rb') as source:
                if source.read(16) != salt:
                    raise NativeSchemaError('Native database keys belong to a different salt')
        self._connections, self._updated = {}, {}
        self.bindings = {}
        self._schema_checked = set()

    def connection(self, relative, fresh=False):
        now = time.monotonic()
        if fresh or now-self._updated.get(relative, -1e9) > .2:
            self._connections[relative] = self.databases[relative].refresh()
            self._updated[relative] = now
        return self._connections[relative]

    @staticmethod
    def require(connection, table, columns):
        actual = {row[1] for row in connection.execute('SELECT * FROM pragma_table_info(?)', (table,))}
        if not set(columns) <= actual:
            raise NativeSchemaError('The installed WeChat database schema is unsupported')

    def contacts(self):
        connection = self.connection('contact/contact.db')
        self.require(connection, 'contact', ['username', 'remark', 'nick_name'])
        return {row['username']: dict(row) for row in connection.execute('SELECT username,remark,nick_name FROM contact')}

    def bind(self, chat_id, username):
        if not chat_id.startswith('name:') or not username or (chat_id in self.bindings and self.bindings[chat_id] != username):
            raise NativeSchemaError('A native chat binding cannot be reassigned')
        if username in self.bindings.values() and self.bindings.get(chat_id) != username:
            raise NativeSchemaError('More than one setting targets the same native conversation')
        self.bindings[chat_id] = username

    def list_chats(self):
        contacts = self.contacts()
        connection = self.connection('session/session.db')
        self.require(connection, 'SessionTable', ['username', 'unread_count', 'summary', 'last_timestamp', 'is_hidden'])
        reverse = {username: legacy for legacy, username in self.bindings.items()}
        result = []
        for row in connection.execute('SELECT username,unread_count,summary,last_timestamp FROM SessionTable WHERE is_hidden=0 ORDER BY sort_timestamp DESC'):
            username = row['username']
            contact = contacts.get(username, {})
            name = contact.get('remark') or contact.get('nick_name') or ('File Transfer' if username == 'filehelper' else username)
            try:
                summary = decode_field(row['summary'])
            except NativeSchemaError:
                summary = '[Unread activity]'
            result.append({'id': reverse.get(username, 'native:'+username), 'native_id': username, 'name': name,
                           'chat_kind': 'group' if username.endswith('@chatroom') else 'direct',
                           'unread': int(row['unread_count'] or 0), 'preview': summary[:1200],
                           'last_timestamp': row['last_timestamp']})
        return result

    def resolve(self, target):
        chats = self.list_chats()
        if target in self.bindings:
            matches = [chat for chat in chats if chat['native_id'] == self.bindings[target]]
        else:
            query = target.removeprefix('name:')
            matches = [chat for chat in chats if target == chat['id'] or target == 'native:'+chat['native_id'] or query == chat['name']]
        if len(matches) != 1:
            raise NativeSchemaError('Native recipient is unavailable or ambiguous; use --chats and an exact ID')
        return matches[0]

    def read_messages(self, target, limit=20, fresh=False):
        if any(target_path(path.relative_to(self.root).as_posix()) and path.relative_to(self.root).as_posix() not in self.databases
               for path in (self.root/'message').glob('message_*.db')):
            raise NativeSchemaError('A new native message shard needs a validated local database key')
        chat = self.resolve(target)
        table = 'Msg_'+hashlib.md5(chat['native_id'].encode()).hexdigest()
        contacts = self.contacts()
        result = []
        for relative in sorted(self.databases):
            if not relative.startswith('message/'):
                continue
            connection = self.connection(relative, fresh=fresh)
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            self.require(connection, table, ['local_id','local_type','real_sender_id','create_time','sort_seq',
                                             'message_content','source','WCDB_CT_source','WCDB_CT_message_content'])
            self.require(connection, 'Name2Id', ['user_name'])
            senders = {row[0]: row[1] for row in connection.execute('SELECT rowid,user_name FROM Name2Id')}
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY sort_seq DESC,local_id DESC LIMIT ?',
                                      (max(1,min(int(limit),100)),)).fetchall()
            for row in rows:
                sender = senders.get(row['real_sender_id'])
                contact = contacts.get(sender, {})
                outgoing = sender == self.username if sender else None
                kind = {1:'text',3:'image',34:'voice',43:'video',47:'sticker',49:'file'}.get(row['local_type'] & 0xffffffff, 'unsupported')
                content, mentioned, reference_id = '', False, None
                try:
                    content = decode_field(row['message_content'], row['WCDB_CT_message_content'])
                    source = decode_field(row['source'], row['WCDB_CT_source'])
                    mentioned = mentions_account(source, self.username) if chat['chat_kind']=='group' else False
                    if chat['chat_kind']=='group' and sender and content.startswith(sender+':\n'):
                        content = content[len(sender)+2:]
                    if row['local_type'] & 0xffffffff == 49:
                        quote = quoted_text(content)
                        if quote:
                            content, reference_id = quote
                            kind = 'text'
                except NativeSchemaError:
                    kind, mentioned, content = 'unsupported', None, '[Unsupported activity]'
                if kind != 'text':
                    content = '['+kind.capitalize()+']'
                result.append({'id': f"native:{relative}:{table}:{row['local_id']}", 'chat_id':chat['id'],
                               'sender_id':sender or '', 'sender_name':contact.get('remark') or contact.get('nick_name') or sender or '',
                               'text':content, 'outgoing':outgoing, 'kind':kind, 'timestamp':row['create_time'],
                               'position':[row['sort_seq'],row['local_id']], 'server_id':str(row['server_id']) if 'server_id' in row.keys() else '',
                               'reply_to_server_id':reference_id,
                               'chat_kind':chat['chat_kind'], 'mentions_me':mentioned, 'mention_names':None,
                               '_sort':(row['sort_seq'],row['local_id'])})
        return sorted(result, key=lambda item: item['_sort'])[-max(1,min(int(limit),100)):]

    def close(self):
        for database in self.databases.values():
            database.close()
