"""Check native database readiness without displaying messages or key values.

This diagnostic does not enable replies or replace the desktop transport. Run as the bot user:
    venv/bin/python -m extensions.wechat.probe --database-root /path/to/wxid_x/db_storage --keys <key file>
"""
import argparse
import json
from pathlib import Path
import time

from .database_keys import load_keys
from .database_pages import DatabaseSnapshotError, EncryptedDatabase


def probe(root, key_file, include_schema=False):
    root = root.resolve(strict=True)
    if root.name != 'db_storage' or not root.parent.name.startswith('wxid_'):
        raise DatabaseSnapshotError('Select one native account db_storage directory')
    keys = load_keys(key_file, root.parent.name)
    results = []
    for relative, (salt, key) in sorted(keys.items()):
        path = root / relative
        with path.open('rb') as source:
            if source.read(16) != salt:
                raise DatabaseSnapshotError('The database salt changed; refresh the account keys')
        reader = EncryptedDatabase(path, key)
        started = time.monotonic()
        try:
            connection = reader.refresh()
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise DatabaseSnapshotError('The decrypted native database failed its integrity check')
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            result = {'path': relative, 'integrity': 'ok', 'table_count': len(tables),
                      'snapshot_ms': round((time.monotonic()-started)*1000, 1)}
            if include_schema:
                result['schema'] = {table[0]: [column[1] for column in connection.execute(
                    'SELECT * FROM pragma_table_info(?)', (table[0],))] for table in tables}
            results.append(result)
        finally:
            reader.close()
    return {'decryption_ready': True, 'diagnostic_only': True, 'databases': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-root', type=Path, required=True)
    parser.add_argument('--keys', type=Path, required=True)
    parser.add_argument('--schema', action='store_true', help='Show table/column names only')
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.database_root, args.keys, args.schema), ensure_ascii=False))
    except (DatabaseSnapshotError, OSError) as error:
        print(json.dumps({'decryption_ready': False, 'diagnostic_only': True, 'error': str(error)}))
        raise SystemExit(1)
