"""
Configuration module for IX.br Status Bot.
Uses Pydantic for validation and standard logging format.
"""

import sys
import logging
from pathlib import Path
from typing import Optional
from datetime import time

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Bot configuration settings with validation.
    Loaded from environment variables or .env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Bot Token (required)
    telegram_bot_token: str = Field(
        ...,
        description="Telegram bot token from @BotFather"
    )

    # RSS Feed URL
    rss_feed_url: str = Field(
        default="https://status.ix.br/rss",
        description="IX.br status RSS feed URL"
    )

    # Check interval in seconds
    check_interval: int = Field(
        default=300,
        ge=60,
        description="Interval between RSS checks (minimum 60 seconds)"
    )

    # Maximum message age in days
    max_message_age_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Maximum age for messages (1-30 days)"
    )

    # Database path
    database_path: str = Field(
        default="/app/data/ixbr_bot.db",
        description="Path to SQLite database"
    )

    # Log level
    log_level: str = Field(
        default="INFO",
        description="Logging level"
    )

    # Health check file path
    health_check_file: str = Field(
        default="/app/data/health",
        description="Path to health check file"
    )

    # Rate limiting: max commands per minute per chat
    rate_limit_commands: int = Field(
        default=10,
        ge=1,
        description="Maximum commands per minute per chat"
    )

    # Quiet hours (optional)
    quiet_hours_start: Optional[str] = Field(
        default=None,
        description="Start of quiet hours (HH:MM format, e.g., '22:00')"
    )

    quiet_hours_end: Optional[str] = Field(
        default=None,
        description="End of quiet hours (HH:MM format, e.g., '07:00')"
    )

    # Admin user IDs (comma-separated) - can use /backup, /restore, /broadcast
    admin_user_ids: str = Field(
        default="",
        description="Comma-separated list of Telegram user IDs with admin access"
    )

    # Auto-backup settings
    backup_enabled: bool = Field(
        default=False,
        description="Enable automatic daily backups"
    )

    backup_chat_id: Optional[int] = Field(
        default=None,
        description="Chat ID to send automatic backups to"
    )

    # Maximum backup file size in bytes (default: 1MB)
    max_backup_size: int = Field(
        default=1048576,
        description="Maximum backup file size in bytes"
    )

    @field_validator("backup_chat_id", mode="before")
    @classmethod
    def validate_backup_chat_id(cls, v):
        """Handle empty string as None."""
        if v == "" or v is None:
            return None
        return int(v)

    def get_admin_ids(self) -> set[int]:
        """Get set of admin user IDs."""
        if not self.admin_user_ids:
            return set()
        
        try:
            return {
                int(uid.strip()) 
                for uid in self.admin_user_ids.split(",") 
                if uid.strip().isdigit()
            }
        except ValueError:
            return set()

    def is_admin(self, user_id: int) -> bool:
        """Check if a user ID is an admin."""
        return user_id in self.get_admin_ids()

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        """Validate Telegram bot token format."""
        if not v:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        # Basic format check: number:alphanumeric
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError(
                "Invalid token format. Expected: 123456789:ABCdefGHI..."
            )

        if not parts[0].isdigit():
            raise ValueError("Token should start with bot ID (numbers)")

        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_time_format(cls, v: Optional[str]) -> Optional[str]:
        """Validate time format (HH:MM)."""
        if v is None:
            return None

        try:
            parts = v.split(":")
            if len(parts) != 2:
                raise ValueError()
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError()
            return v
        except (ValueError, AttributeError):
            raise ValueError(
                f"Invalid time format: {v}. Expected HH:MM (e.g., '22:00')"
            )

    def get_quiet_hours(self) -> Optional[tuple[time, time]]:
        """
        Get quiet hours as time objects.
        Returns None if quiet hours are not configured.
        """
        if not self.quiet_hours_start or not self.quiet_hours_end:
            return None

        start_parts = self.quiet_hours_start.split(":")
        end_parts = self.quiet_hours_end.split(":")

        start = time(int(start_parts[0]), int(start_parts[1]))
        end = time(int(end_parts[0]), int(end_parts[1]))

        return (start, end)

    def ensure_data_directory(self) -> None:
        """Ensure the data directory exists."""
        db_path = Path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        health_path = Path(self.health_check_file)
        health_path.parent.mkdir(parents=True, exist_ok=True)


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """
    Configure standard logging with clean, readable format.
    
    Format: YYYY-MM-DD HH:MM:SS | LEVEL | module | message
    
    Returns the configured logger instance.
    """
    # Get numeric log level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Create formatter with clean, readable format
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # Get logger for the application
    logger = logging.getLogger("ixbr_bot")
    logger.setLevel(numeric_level)

    return logger


# Load settings (will validate on import)
try:
    config = Settings()
except Exception as e:
    print(f"Configuration error: {e}", file=sys.stderr)
    print("Please check your .env file or environment variables.", file=sys.stderr)
    sys.exit(1)

# Setup logging with configured level
logger = setup_logging(config.log_level)
