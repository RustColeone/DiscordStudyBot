import sqlite3
import json
import datetime
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager

DATABASE_PATH = 'bot_data.db'
DEFAULT_LLM = "deepseek"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_EFFORT = "high"

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
                active_model TEXT DEFAULT 'deepseek-v4-flash',
                listen_mode INTEGER DEFAULT 0,
                effort_level TEXT DEFAULT 'high',
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

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

def mark_reminder_sent(reminder_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (reminder_id,))

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
            SELECT active_llm, active_model, listen_mode, effort_level
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
            }
        else:
            # Return defaults if not set
            return {
                "llm": DEFAULT_LLM,
                "model": DEFAULT_MODEL,
                "listen_mode": False,
                "effort": DEFAULT_EFFORT,
            }

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
