import sqlite3
import json
import datetime
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager

DATABASE_PATH = 'bot_data.db'
DEFAULT_LLM = "deepseek"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_EFFORT = "high"
DEFAULT_CHAT_MODE = "agent"

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # Enable column access by name
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_database():
    """Initialize database tables"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Chat history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                ai_model TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Index for faster queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_history 
            ON chat_history(channel_id, ai_model, timestamp)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_context_summaries (
                channel_id TEXT NOT NULL,
                ai_model TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, ai_model)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversation_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                attachment_id TEXT,
                author_id TEXT,
                filename TEXT NOT NULL,
                url TEXT NOT NULL,
                content_type TEXT,
                size INTEGER,
                created_at TEXT,
                UNIQUE(channel_id, url)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversation_attachments
            ON conversation_attachments(channel_id, id DESC)
        ''')
        cursor.execute("PRAGMA table_info(conversation_attachments)")
        attachment_columns = {row[1] for row in cursor.fetchall()}
        if "attachment_id" not in attachment_columns:
            cursor.execute("ALTER TABLE conversation_attachments ADD COLUMN attachment_id TEXT")
        
        # Music state table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS music_state (
                text_channel_id TEXT PRIMARY KEY,
                voice_channel_id TEXT,
                current_song_index INTEGER DEFAULT 1,
                is_playing INTEGER DEFAULT 0,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Channel settings table for chat configuration
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id TEXT PRIMARY KEY,
                active_llm TEXT DEFAULT 'deepseek',
                active_model TEXT DEFAULT 'deepseek-flash',
                listen_mode INTEGER DEFAULT 0,
                effort_level TEXT DEFAULT 'high',
                chat_mode TEXT DEFAULT 'agent',
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("PRAGMA table_info(channel_settings)")
        channel_setting_columns = {row[1] for row in cursor.fetchall()}
        if "effort_level" not in channel_setting_columns:
            cursor.execute("ALTER TABLE channel_settings ADD COLUMN effort_level TEXT DEFAULT 'high'")
            cursor.execute('''
                UPDATE channel_settings
                SET active_llm = ?, active_model = ?, effort_level = ?, last_updated = CURRENT_TIMESTAMP
                WHERE active_llm = 'chatgpt' AND active_model = 'gpt-3.5-turbo'
            ''', (DEFAULT_LLM, DEFAULT_MODEL, DEFAULT_EFFORT))
        if "chat_mode" not in channel_setting_columns:
            cursor.execute("ALTER TABLE channel_settings ADD COLUMN chat_mode TEXT DEFAULT 'agent'")

        cursor.execute('''
            UPDATE channel_settings
            SET chat_mode = ?, last_updated = CURRENT_TIMESTAMP
            WHERE chat_mode IS NULL OR chat_mode NOT IN ('agent', 'roleplay')
        ''', (DEFAULT_CHAT_MODE,))

        cursor.execute('''
            UPDATE channel_settings
            SET active_model = ?, last_updated = CURRENT_TIMESTAMP
            WHERE active_llm = 'deepseek'
              AND active_model != ?
          ''', (DEFAULT_MODEL, DEFAULT_MODEL))

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                author_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                sent_at TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("PRAGMA table_info(reminders)")
        reminder_columns = {row[1] for row in cursor.fetchall()}
        reminder_migrations = {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_attempt_at": "TEXT",
            "last_error": "TEXT",
            "sent_at": "TEXT",
        }
        for column_name, definition in reminder_migrations.items():
            if column_name not in reminder_columns:
                cursor.execute(f"ALTER TABLE reminders ADD COLUMN {column_name} {definition}")

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(status, remind_at)
        ''')
        
        print("Database initialized successfully")

# ==================== Chat History Functions ====================

def save_chat_message(channel_id: str, ai_model: str, role: str, content: str):
    """Save a single chat message to database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chat_history (channel_id, ai_model, role, content)
            VALUES (?, ?, ?, ?)
        ''', (str(channel_id), ai_model, role, content))

def replace_initial_system_prompt(channel_id: str, ai_model: str, old_content: str,
                                  new_content: str) -> bool:
    """Replace the first system prompt only when it still matches an old default."""
    with get_db() as conn:
        cursor = conn.execute('''
            UPDATE chat_history
            SET content = ?
            WHERE id = (
                SELECT id FROM chat_history
                WHERE channel_id = ? AND ai_model = ? AND role = 'system'
                ORDER BY id ASC
                LIMIT 1
            ) AND content = ?
        ''', (new_content, str(channel_id), ai_model, old_content))
        return cursor.rowcount > 0

def load_chat_history(channel_id: str, ai_model: str) -> List[Dict[str, str]]:
    """Load raw history with any compressed context after the main system prompt."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT role, content
            FROM chat_history
            WHERE channel_id = ? AND ai_model = ?
            ORDER BY id ASC
        ''', (str(channel_id), ai_model))
        rows = cursor.fetchall()
        history = [{"role": row["role"], "content": row["content"]} for row in rows]
        summary = _get_chat_context_summary(cursor, str(channel_id), ai_model)
        if summary:
            summary_message = {
                "role": "system",
                "content": "Compressed conversation context:\n" + summary,
            }
            insert_at = 1 if history and history[0]["role"] == "system" else 0
            history.insert(insert_at, summary_message)
        return history

def load_chat_history_records(channel_id: str, ai_model: str) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT id, role, content
            FROM chat_history
            WHERE channel_id = ? AND ai_model = ?
            ORDER BY id ASC
        ''', (str(channel_id), ai_model)).fetchall()
        return [dict(row) for row in rows]

def _get_chat_context_summary(cursor, channel_id: str, ai_model: str) -> Optional[str]:
    row = cursor.execute('''
        SELECT content FROM chat_context_summaries
        WHERE channel_id = ? AND ai_model = ?
    ''', (str(channel_id), ai_model)).fetchone()
    return row["content"] if row else None

def get_chat_context_summary(channel_id: str, ai_model: str) -> Optional[str]:
    with get_db() as conn:
        return _get_chat_context_summary(conn.cursor(), str(channel_id), ai_model)

def replace_chat_history_with_summary(channel_id: str, ai_model: str, summary: str,
                                      message_ids: List[int]) -> None:
    if not message_ids:
        return
    with get_db() as conn:
        conn.execute('''
            INSERT INTO chat_context_summaries (channel_id, ai_model, content, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id, ai_model) DO UPDATE SET
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
        ''', (str(channel_id), ai_model, summary))
        placeholders = ",".join("?" for _ in message_ids)
        conn.execute(
            f"DELETE FROM chat_history WHERE channel_id = ? AND ai_model = ? AND id IN ({placeholders})",
            (str(channel_id), ai_model, *message_ids),
        )

def clear_chat_history(channel_id: str, ai_model: str):
    """Clear all chat history for a specific channel and AI model"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM chat_history
            WHERE channel_id = ? AND ai_model = ?
        ''', (str(channel_id), ai_model))
        cursor.execute('''
            DELETE FROM chat_context_summaries
            WHERE channel_id = ? AND ai_model = ?
        ''', (str(channel_id), ai_model))
        print(f"Cleared chat history for channel {channel_id}, AI {ai_model}")

def get_chat_history_count(channel_id: str, ai_model: str) -> int:
    """Get the number of messages in history"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) as count
            FROM chat_history
            WHERE channel_id = ? AND ai_model = ?
        ''', (str(channel_id), ai_model))
        return cursor.fetchone()["count"]


def save_conversation_attachments(channel_id: str, attachments: List[Dict]) -> List[int]:
    """Persist Discord attachment references without downloading their content."""
    if not attachments:
        return []
    attachment_ids = []
    with get_db() as conn:
        for attachment in attachments:
            existing = None
            if attachment.get("message_id") and attachment.get("attachment_id"):
                existing = conn.execute('''
                    SELECT id FROM conversation_attachments
                    WHERE channel_id = ? AND message_id = ? AND attachment_id = ?
                ''', (
                    str(channel_id),
                    attachment["message_id"],
                    attachment["attachment_id"],
                )).fetchone()
            if existing:
                conn.execute('''
                    UPDATE conversation_attachments
                    SET url = ?, filename = ?, content_type = ?, size = ?,
                        author_id = COALESCE(?, author_id),
                        created_at = COALESCE(?, created_at)
                    WHERE id = ?
                ''', (
                    attachment["url"],
                    attachment["filename"],
                    attachment.get("content_type"),
                    attachment.get("size"),
                    attachment.get("author_id"),
                    attachment.get("created_at"),
                    existing["id"],
                ))
                attachment_ids.append(existing["id"])
                continue
            conn.execute('''
                INSERT INTO conversation_attachments (
                    channel_id, message_id, attachment_id, author_id, filename, url,
                    content_type, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, url) DO UPDATE SET
                    message_id = COALESCE(excluded.message_id, message_id),
                    attachment_id = COALESCE(excluded.attachment_id, attachment_id),
                    author_id = COALESCE(excluded.author_id, author_id),
                    filename = excluded.filename,
                    content_type = COALESCE(excluded.content_type, content_type),
                    size = COALESCE(excluded.size, size),
                    created_at = COALESCE(excluded.created_at, created_at)
            ''', (
                str(channel_id),
                attachment.get("message_id"),
                attachment.get("attachment_id"),
                attachment.get("author_id"),
                attachment["filename"],
                attachment["url"],
                attachment.get("content_type"),
                attachment.get("size"),
                attachment.get("created_at"),
            ))
            row = conn.execute('''
                SELECT id FROM conversation_attachments
                WHERE channel_id = ? AND url = ?
            ''', (str(channel_id), attachment["url"])).fetchone()
            attachment_ids.append(row["id"])
    return attachment_ids


def list_conversation_attachments(channel_id: str, limit: int = 20) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT id, channel_id, message_id, attachment_id, author_id, filename,
                   url, content_type, size, created_at
            FROM conversation_attachments
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
        ''', (str(channel_id), max(1, int(limit)))).fetchall()
        return [dict(row) for row in rows]


def find_conversation_attachment(channel_id: str, selector: str) -> Optional[Dict]:
    """Resolve an attachment by durable ID, filename, or the latest shorthand."""
    normalized = str(selector or "latest").strip()
    with get_db() as conn:
        if normalized.lower() == "latest":
            row = conn.execute('''
                SELECT id, channel_id, message_id, attachment_id, author_id, filename,
                       url, content_type, size, created_at
                FROM conversation_attachments
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT 1
            ''', (str(channel_id),)).fetchone()
        elif normalized.isdigit():
            row = conn.execute('''
                SELECT id, channel_id, message_id, attachment_id, author_id, filename,
                       url, content_type, size, created_at
                FROM conversation_attachments
                WHERE channel_id = ? AND id = ?
            ''', (str(channel_id), int(normalized))).fetchone()
        else:
            row = conn.execute('''
                SELECT id, channel_id, message_id, attachment_id, author_id, filename,
                       url, content_type, size, created_at
                FROM conversation_attachments
                WHERE channel_id = ? AND filename = ?
                ORDER BY id DESC
                LIMIT 1
            ''', (str(channel_id), normalized)).fetchone()
        return dict(row) if row else None


def update_conversation_attachment_url(attachment_id: int, url: str) -> None:
    with get_db() as conn:
        conn.execute('''
            UPDATE conversation_attachments SET url = ? WHERE id = ?
        ''', (url, int(attachment_id)))


def clear_conversation_attachments(channel_id: str) -> None:
    with get_db() as conn:
        conn.execute('''
            DELETE FROM conversation_attachments WHERE channel_id = ?
        ''', (str(channel_id),))

# ==================== Music State Functions ====================

def save_music_state(text_channel_id: str, voice_channel_id: str, 
                     song_index: int, is_playing: bool):
    """Save or update music state for a text channel"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO music_state (text_channel_id, voice_channel_id, current_song_index, is_playing)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(text_channel_id) DO UPDATE SET
                voice_channel_id = ?,
                current_song_index = ?,
                is_playing = ?,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(text_channel_id), str(voice_channel_id), song_index, int(is_playing),
              str(voice_channel_id), song_index, int(is_playing)))

def load_music_state(text_channel_id: str) -> Optional[Dict]:
    """Load music state for a text channel"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT voice_channel_id, current_song_index, is_playing, last_updated
            FROM music_state
            WHERE text_channel_id = ?
        ''', (str(text_channel_id),))
        
        row = cursor.fetchone()
        if row:
            return {
                "voice_channel_id": row["voice_channel_id"],
                "current_song_index": row["current_song_index"],
                "is_playing": bool(row["is_playing"]),
                "last_updated": row["last_updated"]
            }
        return None

def clear_music_state(text_channel_id: str):
    """Clear music state for a text channel"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM music_state
            WHERE text_channel_id = ?
        ''', (str(text_channel_id),))

# ==================== Reminder Functions ====================

def create_reminder(channel_id: str, author_id: str, author_name: str, platform: str,
                    remind_at: datetime.datetime, message: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO reminders (channel_id, author_id, author_name, platform, remind_at, message)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            str(channel_id),
            str(author_id),
            author_name,
            platform,
            remind_at.astimezone(datetime.timezone.utc).isoformat(),
            message,
        ))
        return int(cursor.lastrowid)

def get_due_reminders(now: datetime.datetime) -> List[Dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, channel_id, author_id, author_name, platform, remind_at, message
            FROM reminders
            WHERE status = 'pending' AND remind_at <= ?
            ORDER BY remind_at ASC
        ''', (now.astimezone(datetime.timezone.utc).isoformat(),))
        return [dict(row) for row in cursor.fetchall()]

def get_pending_reminders(channel_id: str, author_id: Optional[str] = None) -> List[Dict]:
    with get_db() as conn:
        if author_id is None:
            rows = conn.execute('''
                SELECT id, channel_id, author_id, author_name, platform, remind_at, message
                FROM reminders
                WHERE channel_id = ? AND status = 'pending'
                ORDER BY remind_at ASC
            ''', (str(channel_id),)).fetchall()
        else:
            rows = conn.execute('''
                SELECT id, channel_id, author_id, author_name, platform, remind_at, message
                FROM reminders
                WHERE channel_id = ? AND author_id = ? AND status = 'pending'
                ORDER BY remind_at ASC
            ''', (str(channel_id), str(author_id))).fetchall()
        return [dict(row) for row in rows]

def delete_pending_reminder(reminder_id: int, channel_id: str, author_id: str,
                            allow_any_author: bool = False) -> bool:
    with get_db() as conn:
        if allow_any_author:
            cursor = conn.execute('''
                DELETE FROM reminders
                WHERE id = ? AND channel_id = ? AND status = 'pending'
            ''', (reminder_id, str(channel_id)))
        else:
            cursor = conn.execute('''
                DELETE FROM reminders
                WHERE id = ? AND channel_id = ? AND author_id = ? AND status = 'pending'
            ''', (reminder_id, str(channel_id), str(author_id)))
        return cursor.rowcount > 0

def mark_reminder_sent(reminder_id: int) -> None:
    with get_db() as conn:
        conn.execute('''
            UPDATE reminders
            SET status = 'sent', sent_at = ?, last_error = NULL
            WHERE id = ?
        ''', (datetime.datetime.now(datetime.timezone.utc).isoformat(), reminder_id))

def record_reminder_attempt(reminder_id: int) -> None:
    with get_db() as conn:
        conn.execute('''
            UPDATE reminders
            SET attempt_count = attempt_count + 1, last_attempt_at = ?, last_error = NULL
            WHERE id = ? AND status = 'pending'
        ''', (datetime.datetime.now(datetime.timezone.utc).isoformat(), reminder_id))

def record_reminder_failure(reminder_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute('''
            UPDATE reminders SET last_error = ?
            WHERE id = ? AND status = 'pending'
        ''', (error[:1000], reminder_id))

# ==================== Export/Import Functions ====================

def export_to_json(filepath: str = "bot_data_export.json"):
    """Export entire database to JSON file for debugging"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Export chat history
        cursor.execute('SELECT * FROM chat_history ORDER BY timestamp ASC')
        chat_history = [dict(row) for row in cursor.fetchall()]
        
        # Export music state
        cursor.execute('SELECT * FROM music_state')
        music_state = [dict(row) for row in cursor.fetchall()]

        cursor.execute('SELECT * FROM chat_context_summaries')
        context_summaries = [dict(row) for row in cursor.fetchall()]
        
        export_data = {
            "export_timestamp": datetime.datetime.now().isoformat(),
            "chat_history": chat_history,
            "music_state": music_state,
            "context_summaries": context_summaries,
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        
        print(f"Database exported to {filepath}")
        return filepath

def import_from_json(filepath: str = "bot_data_export.json", clear_existing: bool = True):
    """Import database from JSON file"""
    with open(filepath, 'r', encoding='utf-8') as f:
        import_data = json.load(f)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        if clear_existing:
            cursor.execute('DELETE FROM chat_history')
            cursor.execute('DELETE FROM chat_context_summaries')
            cursor.execute('DELETE FROM music_state')
            print("Cleared existing data")
        
        # Import chat history
        for msg in import_data.get("chat_history", []):
            cursor.execute('''
                INSERT INTO chat_history (channel_id, ai_model, role, content, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (msg["channel_id"], msg["ai_model"], msg["role"], 
                  msg["content"], msg.get("timestamp")))
        
        # Import music state
        for state in import_data.get("music_state", []):
            cursor.execute('''
                INSERT INTO music_state (text_channel_id, voice_channel_id, 
                                        current_song_index, is_playing, last_updated)
                VALUES (?, ?, ?, ?, ?)
            ''', (state["text_channel_id"], state["voice_channel_id"],
                  state["current_song_index"], state["is_playing"], 
                  state.get("last_updated")))

        for summary in import_data.get("context_summaries", []):
            cursor.execute('''
                INSERT INTO chat_context_summaries (channel_id, ai_model, content, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (
                summary["channel_id"],
                summary["ai_model"],
                summary["content"],
                summary.get("updated_at"),
            ))
        
        print(f"Database imported from {filepath}")

# ==================== Statistics Functions ====================

def get_database_stats() -> Dict:
    """Get statistics about database contents"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM chat_history')
        total_messages = cursor.fetchone()["count"]
        
        cursor.execute('''
            SELECT ai_model, COUNT(*) as count 
            FROM chat_history 
            GROUP BY ai_model
        ''')
        messages_by_ai = {row["ai_model"]: row["count"] for row in cursor.fetchall()}
        
        cursor.execute('SELECT COUNT(*) as count FROM music_state')
        active_music_sessions = cursor.fetchone()["count"]
        
        return {
            "total_messages": total_messages,
            "messages_by_ai": messages_by_ai,
            "active_music_sessions": active_music_sessions
        }

# ==================== Channel Settings Functions ====================

def get_channel_settings(channel_id: str) -> Dict:
    """Get chat settings for a channel (LLM, model, listen mode)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT active_llm, active_model, listen_mode, effort_level, chat_mode
            FROM channel_settings
            WHERE channel_id = ?
        ''', (str(channel_id),))
        
        row = cursor.fetchone()
        if row:
            return {
                "llm": row["active_llm"],
                "model": row["active_model"],
                "listen_mode": bool(row["listen_mode"]),
                "effort": row["effort_level"] or DEFAULT_EFFORT,
                "mode": row["chat_mode"] or DEFAULT_CHAT_MODE,
            }
        else:
            # Return defaults if not set
            return {
                "llm": DEFAULT_LLM,
                "model": DEFAULT_MODEL,
                "listen_mode": False,
                "effort": DEFAULT_EFFORT,
                "mode": DEFAULT_CHAT_MODE,
            }

def set_chat_mode(channel_id: str, mode: str) -> None:
    if mode not in {"agent", "roleplay"}:
        raise ValueError(f"Invalid chat mode: {mode}")
    with get_db() as conn:
        conn.execute('''
            INSERT INTO channel_settings (
                channel_id, active_llm, active_model, effort_level, chat_mode, last_updated
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                chat_mode = excluded.chat_mode,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(channel_id), DEFAULT_LLM, DEFAULT_MODEL, DEFAULT_EFFORT, mode))

def set_channel_llm(channel_id: str, llm_name: str, model_name: str = None):
    """Set the active LLM for a channel"""
    with get_db() as conn:
        cursor = conn.cursor()
        if model_name:
            cursor.execute('''
                INSERT INTO channel_settings (channel_id, active_llm, active_model, last_updated)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel_id) DO UPDATE SET
                    active_llm = excluded.active_llm,
                    active_model = excluded.active_model,
                    last_updated = CURRENT_TIMESTAMP
            ''', (str(channel_id), llm_name, model_name))
        else:
            cursor.execute('''
                INSERT INTO channel_settings (channel_id, active_llm, last_updated)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel_id) DO UPDATE SET
                    active_llm = excluded.active_llm,
                    last_updated = CURRENT_TIMESTAMP
            ''', (str(channel_id), llm_name))

def set_channel_model(channel_id: str, model_name: str):
    """Set the active model for a channel"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO channel_settings (channel_id, active_llm, active_model, effort_level, last_updated)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                active_model = excluded.active_model,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(channel_id), DEFAULT_LLM, model_name, DEFAULT_EFFORT))

def set_effort_level(channel_id: str, effort: str) -> None:
    if effort not in {"low", "medium", "high"}:
        raise ValueError(f"Invalid effort level: {effort}")
    with get_db() as conn:
        conn.execute('''
            INSERT INTO channel_settings (
                channel_id, active_llm, active_model, effort_level, last_updated
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                effort_level = excluded.effort_level,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(channel_id), DEFAULT_LLM, DEFAULT_MODEL, effort))

def set_listen_mode(channel_id: str, enabled: bool) -> None:
    """Set listen mode for a channel to specific state"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO channel_settings (
                channel_id, active_llm, active_model, listen_mode, effort_level, last_updated
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                listen_mode = excluded.listen_mode,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(channel_id), DEFAULT_LLM, DEFAULT_MODEL, int(enabled), DEFAULT_EFFORT))

def toggle_listen_mode(channel_id: str) -> bool:
    """Toggle listen mode for a channel, returns new state (deprecated: use set_listen_mode)"""
    with get_db() as conn:
        cursor = conn.cursor()
        current = get_channel_settings(str(channel_id))
        new_state = not current["listen_mode"]
        
        cursor.execute('''
            INSERT INTO channel_settings (
                channel_id, active_llm, active_model, listen_mode, effort_level, last_updated
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                listen_mode = excluded.listen_mode,
                last_updated = CURRENT_TIMESTAMP
        ''', (str(channel_id), DEFAULT_LLM, DEFAULT_MODEL, int(new_state), DEFAULT_EFFORT))
        
        return new_state

# Initialize database on module import
init_database()
