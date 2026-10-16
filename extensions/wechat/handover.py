"""One-time read position handover from the legacy WeChat bridge (bot_data.db), opened read-only.

The legacy bridge keeps `wechat_read_cursors` per `(account, chat)` and pins `name:` chats to native
usernames in `wechat_native_bindings`. Importing them lets the first start of this extension submit
what arrived while neither process was reading, instead of baselining it away.
"""

import sqlite3
from contextlib import closing
from pathlib import Path


def legacy_cursors(path, account_directory):
    """`{native username: [sort_seq, local_id]}` for the account; empty if the tables are missing."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        try:
            bindings = dict(connection.execute(
                "SELECT chat_id, native_id FROM wechat_native_bindings WHERE account_id=?", (account_directory,)))
        except sqlite3.OperationalError:
            bindings = {}
        try:
            rows = connection.execute("SELECT chat_id, sort_seq, local_id FROM wechat_read_cursors WHERE account_id=?",
                                      (account_directory,)).fetchall()
        except sqlite3.OperationalError:
            return {}
    result = {}
    for chat_id, sort_seq, local_id in rows:
        native = bindings.get(chat_id) or (chat_id.removeprefix("native:") if chat_id.startswith("native:") else None)
        if native:
            result[native] = [int(sort_seq), int(local_id)]
    return result
