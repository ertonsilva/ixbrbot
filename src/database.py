"""
Async database module for IX.br Status Bot.
Uses aiosqlite for non-blocking database operations.
"""

import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from .config import config, logger


class Database:
    """Async SQLite database handler for the bot."""

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the database.

        Args:
            db_path: Path to the SQLite database file.
                     If None, uses the path from config.
        """
        self.db_path = db_path or config.database_path
        self._initialized = False

    async def init(self) -> None:
        """Initialize database tables. Must be called before using the database."""
        if self._initialized:
            return

        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # Enable foreign keys
            await db.execute("PRAGMA foreign_keys = ON")

            # Table for subscribed chats
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribed_chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    chat_title TEXT,
                    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    quiet_hours_start TEXT,
                    quiet_hours_end TEXT,
                    quiet_hours_tz TEXT DEFAULT 'UTC'
                )
            """)

            # Table for sent messages
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_guid TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER,
                    content_hash TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP,
                    message_title TEXT,
                    delivery_status TEXT DEFAULT 'sent',
                    UNIQUE(message_guid, chat_id)
                )
            """)

            # Table for rate limiting
            await db.execute("""
                CREATE TABLE IF NOT EXISTS command_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Table for pending notifications (quiet hours)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_guid TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    event_title TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(message_guid, chat_id)
                )
            """)

            # Indexes
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_sent_messages_guid
                ON sent_messages(message_guid)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_sent_messages_chat
                ON sent_messages(chat_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_command_log_chat_time
                ON command_log(chat_id, timestamp)
            """)

            await db.commit()

            # Run migrations for existing databases
            await self._migrate(db)

        self._initialized = True
        logger.info(f"Database initialized: {self.db_path}")

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Run migrations for existing databases."""
        # Get existing columns
        cursor = await db.execute("PRAGMA table_info(subscribed_chats)")
        columns = {row[1] for row in await cursor.fetchall()}

        # Add quiet hours columns if missing
        if "quiet_hours_start" not in columns:
            await db.execute(
                "ALTER TABLE subscribed_chats ADD COLUMN quiet_hours_start TEXT"
            )
            logger.info("Migration: Added quiet_hours_start column")

        if "quiet_hours_end" not in columns:
            await db.execute(
                "ALTER TABLE subscribed_chats ADD COLUMN quiet_hours_end TEXT"
            )
            logger.info("Migration: Added quiet_hours_end column")

        if "quiet_hours_tz" not in columns:
            await db.execute(
                "ALTER TABLE subscribed_chats ADD COLUMN quiet_hours_tz TEXT DEFAULT 'UTC'"
            )
            logger.info("Migration: Added quiet_hours_tz column")

        # Check sent_messages columns
        cursor = await db.execute("PRAGMA table_info(sent_messages)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "telegram_message_id" not in columns:
            await db.execute(
                "ALTER TABLE sent_messages ADD COLUMN telegram_message_id INTEGER"
            )
        if "content_hash" not in columns:
            await db.execute(
                "ALTER TABLE sent_messages ADD COLUMN content_hash TEXT"
            )
        if "updated_at" not in columns:
            await db.execute(
                "ALTER TABLE sent_messages ADD COLUMN updated_at TIMESTAMP"
            )
        if "delivery_status" not in columns:
            await db.execute(
                "ALTER TABLE sent_messages ADD COLUMN delivery_status TEXT DEFAULT 'sent'"
            )

        await db.commit()

    # ==================== Chat Subscription Methods ====================

    async def subscribe_chat(
        self,
        chat_id: int,
        chat_type: str,
        chat_title: Optional[str] = None
    ) -> bool:
        """
        Subscribe a chat to receive status updates.
        Returns True if new subscription, False if already subscribed.
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Check if already subscribed
            cursor = await db.execute(
                "SELECT is_active FROM subscribed_chats WHERE chat_id = ?",
                (chat_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                if existing[0]:  # is_active
                    logger.debug(f"Chat already subscribed: {chat_id}")
                    return False
                else:
                    # Reactivate
                    await db.execute(
                        """UPDATE subscribed_chats
                           SET is_active = 1, subscribed_at = CURRENT_TIMESTAMP,
                               chat_title = ?
                           WHERE chat_id = ?""",
                        (chat_title, chat_id)
                    )
                    await db.commit()
                    logger.info(f"Chat resubscribed: {chat_id}")
                    return True

            # New subscription
            await db.execute(
                """INSERT INTO subscribed_chats (chat_id, chat_type, chat_title)
                   VALUES (?, ?, ?)""",
                (chat_id, chat_type, chat_title)
            )
            await db.commit()
            logger.info(f"Chat subscribed: {chat_id} ({chat_type})")
            return True

    async def unsubscribe_chat(self, chat_id: int) -> bool:
        """Unsubscribe a chat. Returns True if was subscribed."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE subscribed_chats
                   SET is_active = 0
                   WHERE chat_id = ? AND is_active = 1""",
                (chat_id,)
            )
            await db.commit()

            if cursor.rowcount > 0:
                logger.info(f"Chat unsubscribed: {chat_id}")
                return True
            return False

    async def get_active_chats(self) -> list[dict]:
        """Get list of all active subscribed chats with their settings."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT chat_id, chat_type, quiet_hours_start, quiet_hours_end, quiet_hours_tz
                   FROM subscribed_chats WHERE is_active = 1"""
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def is_chat_subscribed(self, chat_id: int) -> bool:
        """Check if a chat is currently subscribed."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM subscribed_chats WHERE chat_id = ? AND is_active = 1",
                (chat_id,)
            )
            return await cursor.fetchone() is not None

    async def set_quiet_hours(
        self,
        chat_id: int,
        start: Optional[str],
        end: Optional[str],
        timezone: Optional[str] = "UTC"
    ) -> bool:
        """Set quiet hours for a chat. Pass None to disable."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE subscribed_chats
                   SET quiet_hours_start = ?, quiet_hours_end = ?, quiet_hours_tz = ?
                   WHERE chat_id = ? AND is_active = 1""",
                (start, end, timezone, chat_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_chat_quiet_hours(
        self,
        chat_id: int
    ) -> Optional[tuple[str, str, str]]:
        """Get quiet hours for a chat. Returns (start, end, timezone) or None."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT quiet_hours_start, quiet_hours_end, quiet_hours_tz
                   FROM subscribed_chats WHERE chat_id = ?""",
                (chat_id,)
            )
            row = await cursor.fetchone()
            if row and row[0] and row[1]:
                return (row[0], row[1], row[2] or "UTC")
            return None

    # ==================== Sent Messages Methods ====================

    async def get_sent_message(
        self,
        message_guid: str,
        chat_id: int
    ) -> Optional[dict]:
        """Get information about a sent message."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT telegram_message_id, content_hash, message_title,
                          delivery_status
                   FROM sent_messages
                   WHERE message_guid = ? AND chat_id = ?""",
                (message_guid, chat_id)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def mark_message_sent(
        self,
        message_guid: str,
        chat_id: int,
        telegram_message_id: int,
        content_hash: str,
        message_title: Optional[str] = None,
        delivery_status: str = "sent"
    ) -> None:
        """Mark a message as sent to a specific chat."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO sent_messages
                   (message_guid, chat_id, telegram_message_id, content_hash,
                    message_title, sent_at, delivery_status)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                (message_guid, chat_id, telegram_message_id, content_hash,
                 message_title, delivery_status)
            )
            await db.commit()

            logger.debug(f"Message marked as sent: chat={chat_id} msg_id={telegram_message_id}")

    async def update_message_record(
        self,
        message_guid: str,
        chat_id: int,
        content_hash: str,
        message_title: Optional[str] = None
    ) -> None:
        """Update the record of a sent message after editing."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE sent_messages
                   SET content_hash = ?, message_title = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE message_guid = ? AND chat_id = ?""",
                (content_hash, message_title, message_guid, chat_id)
            )
            await db.commit()

    async def update_delivery_status(
        self,
        message_guid: str,
        chat_id: int,
        status: str,
        error_message: Optional[str] = None
    ) -> None:
        """Update delivery status of a message."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE sent_messages
                   SET delivery_status = ?
                   WHERE message_guid = ? AND chat_id = ?""",
                (status, message_guid, chat_id)
            )
            await db.commit()

            if status == "failed":
                logger.warning(f"Message delivery failed: chat={chat_id} error={error_message}")

    async def cleanup_old_messages(self, days: Optional[int] = None) -> int:
        """Remove old sent message records."""
        if days is None:
            days = config.max_message_age_days * 2

        cutoff = datetime.now() - timedelta(days=days)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM sent_messages WHERE sent_at < ?",
                (cutoff.isoformat(),)
            )
            await db.commit()
            deleted = cursor.rowcount

            if deleted > 0:
                logger.info(f"Cleaned up old messages: {deleted}")
            return deleted

    # ==================== Rate Limiting Methods ====================

    async def log_command(self, chat_id: int, command: str) -> None:
        """Log a command for rate limiting."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO command_log (chat_id, command) VALUES (?, ?)",
                (chat_id, command)
            )
            await db.commit()

    async def get_command_count(
        self,
        chat_id: int,
        seconds: int = 60
    ) -> int:
        """Get number of commands from a chat in the last N seconds."""
        cutoff = datetime.now() - timedelta(seconds=seconds)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM command_log
                   WHERE chat_id = ? AND timestamp > ?""",
                (chat_id, cutoff.isoformat())
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def cleanup_command_log(self, seconds: int = 300) -> None:
        """Clean up old command log entries."""
        cutoff = datetime.now() - timedelta(seconds=seconds)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM command_log WHERE timestamp < ?",
                (cutoff.isoformat(),)
            )
            await db.commit()

    # ==================== Pending Notifications (Quiet Hours) ====================

    async def add_pending_notification(
        self,
        chat_id: int,
        message_guid: str,
        message_text: str,
        event_title: Optional[str] = None
    ) -> None:
        """Add a notification to be sent later (during quiet hours)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO pending_notifications
                   (chat_id, message_guid, message_text, event_title)
                   VALUES (?, ?, ?, ?)""",
                (chat_id, message_guid, message_text, event_title)
            )
            await db.commit()

            logger.debug(f"Added pending notification: chat={chat_id}")

    async def get_pending_notifications(
        self,
        chat_id: int
    ) -> list[dict]:
        """Get all pending notifications for a chat."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT id, message_guid, message_text, event_title, created_at
                   FROM pending_notifications
                   WHERE chat_id = ?
                   ORDER BY created_at ASC""",
                (chat_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def clear_pending_notifications(self, chat_id: int) -> int:
        """Clear all pending notifications for a chat."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM pending_notifications WHERE chat_id = ?",
                (chat_id,)
            )
            await db.commit()
            return cursor.rowcount

    # ==================== Statistics ====================

    async def get_stats(self) -> dict:
        """Get database statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM subscribed_chats WHERE is_active = 1"
            )
            active_chats = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT COUNT(*) FROM sent_messages")
            total_messages = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM sent_messages WHERE delivery_status = 'failed'"
            )
            failed_messages = (await cursor.fetchone())[0]

            return {
                "active_chats": active_chats,
                "total_messages_sent": total_messages,
                "failed_deliveries": failed_messages
            }

    # ==================== Backup / Restore ====================

    async def export_backup(self) -> dict:
        """
        Export all subscribed chats for backup.
        
        Returns:
            Dictionary with backup data and metadata
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            # Export subscribed chats
            cursor = await db.execute(
                """SELECT chat_id, chat_type, chat_title, subscribed_at,
                          is_active, quiet_hours_start, quiet_hours_end, quiet_hours_tz
                   FROM subscribed_chats"""
            )
            chats = [dict(row) for row in await cursor.fetchall()]
            
            # Get stats
            stats = await self.get_stats()
            
            backup = {
                "version": "1.0",
                "exported_at": datetime.now().isoformat(),
                "stats": stats,
                "subscribed_chats": chats
            }
            
            logger.info(f"Backup exported: {len(chats)} chats ({stats['active_chats']} active)")
            
            return backup

    async def import_backup(
        self,
        backup_data: dict,
        merge: bool = True
    ) -> dict:
        """
        Import chats from a backup.
        
        Args:
            backup_data: Backup dictionary from export_backup
            merge: If True, merge with existing. If False, replace all.
            
        Returns:
            Dictionary with import results
        """
        if "subscribed_chats" not in backup_data:
            raise ValueError("Invalid backup format: missing subscribed_chats")
        
        chats = backup_data["subscribed_chats"]
        imported = 0
        skipped = 0
        errors = 0
        
        async with aiosqlite.connect(self.db_path) as db:
            if not merge:
                # Clear existing subscriptions
                await db.execute("DELETE FROM subscribed_chats")
                await db.commit()
                logger.warning("Cleared existing subscriptions for restore")
            
            for chat in chats:
                try:
                    chat_id = chat.get("chat_id")
                    if not chat_id:
                        errors += 1
                        continue
                    
                    # Check if exists
                    cursor = await db.execute(
                        "SELECT 1 FROM subscribed_chats WHERE chat_id = ?",
                        (chat_id,)
                    )
                    exists = await cursor.fetchone()
                    
                    if exists and merge:
                        skipped += 1
                        continue
                    
                    # Insert or replace
                    await db.execute(
                        """INSERT OR REPLACE INTO subscribed_chats
                           (chat_id, chat_type, chat_title, subscribed_at,
                            is_active, quiet_hours_start, quiet_hours_end, quiet_hours_tz)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            chat_id,
                            chat.get("chat_type", "unknown"),
                            chat.get("chat_title"),
                            chat.get("subscribed_at", datetime.now().isoformat()),
                            chat.get("is_active", 1),
                            chat.get("quiet_hours_start"),
                            chat.get("quiet_hours_end"),
                            chat.get("quiet_hours_tz", "UTC")
                        )
                    )
                    imported += 1
                    
                except Exception as e:
                    logger.error(f"Error importing chat: {e}")
                    errors += 1
            
            await db.commit()
        
        result = {
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "total_in_backup": len(chats)
        }
        
        logger.info(f"Backup imported: {imported} imported, {skipped} skipped, {errors} errors")
        return result
