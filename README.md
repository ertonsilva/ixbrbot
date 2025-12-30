# IX.br Status Bot

A Telegram bot that monitors the [IX.br status page](https://status.ix.br) and sends real-time notifications about network incidents, scheduled maintenance windows, and resolved issues.

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Features

- **Real-time monitoring** of IX.br status RSS feed with automatic retry on failures
- **Automatic notifications** for:
  - Network incidents and outages
  - Scheduled maintenance windows
  - Resolved issues
- **Smart message updates** - when an event is updated on IX.br, the bot edits the original message instead of sending a new one
- **Quiet hours** - configure time periods when notifications are queued and sent as a summary later
- **Rate limiting** - protection against command spam
- **Multi-chat support** - works in groups and private chats
- **Duplicate prevention** - async SQLite database tracks sent messages
- **Age filtering** - only sends events from the last 7 days (configurable)
- **Structured logging** - JSON logs for easy parsing and monitoring
- **Docker ready** - easy deployment with health checks and graceful shutdown
- **Graceful handling** - automatically unsubscribes inaccessible chats

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- A Telegram Bot Token (get one from [@BotFather](https://t.me/BotFather))

### Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourusername/ixbrbot.git
   cd ixbrbot
   ```

2. **Check environment and initialize configuration**

   ```bash
   ./setup.sh check
   ./setup.sh init
   ./setup.sh config --token "YOUR_BOT_TOKEN_HERE"
   ```

3. **Start the bot**

   ```bash
   ./setup.sh start
   ```

4. **Check logs**

   ```bash
   ./setup.sh logs -f
   ```

## Management Script (setup.sh)

The `setup.sh` script is the single entry point for managing the bot.

### Management Commands

```bash
./setup.sh start      # Start the bot
./setup.sh stop       # Stop the bot
./setup.sh restart    # Restart the bot
./setup.sh rebuild    # Rebuild image and restart
./setup.sh logs       # View logs (last 100 lines)
./setup.sh logs -f    # Follow logs in real-time
./setup.sh ps         # Show container status
./setup.sh shell      # Open shell inside container
```

### Configuration Commands

```bash
./setup.sh init       # Create initial .env file
./setup.sh config     # Show current configuration
./setup.sh show       # Same as config
./setup.sh check      # Validate environment and configuration
```

### Configuration Options

```bash
./setup.sh config --token "123456:ABC..."       # Telegram bot token
./setup.sh config --interval 600                # Check interval (seconds)
./setup.sh config --max-age 14                  # Max event age (days)
./setup.sh config --log-level DEBUG             # Log level
./setup.sh config --rate-limit 5                # Commands per minute limit
./setup.sh config --quiet-hours 22:00 07:00     # Set quiet hours
./setup.sh config --quiet-hours off             # Disable quiet hours
./setup.sh config --admin 123456789             # Add admin user ID
./setup.sh config --backup-chat -1001234567890  # Enable auto backup
```

## Bot Commands

### Public Commands

| Command     | Description                              |
|-------------|------------------------------------------|
| `/start`    | Subscribe to receive notifications       |
| `/stop`     | Unsubscribe from notifications           |
| `/status`   | Check bot and feed status                |
| `/silencio` | Configure quiet hours                    |
| `/help`     | Show help and information                |

### Admin Commands

| Command    | Description                              |
|------------|------------------------------------------|
| `/backup`  | Export subscriptions as JSON file        |
| `/restore` | Import subscriptions from backup file    |
| `/stats`   | Show detailed statistics                 |

### /status output example

```
Status do Bot

Bot: Online
Feed RSS (status.ix.br): Acessivel

Ultimo post: 29/12/2025 as 14:30
IX.br Curitiba, PR - Indisponibilidade de PIX resolvida

Este chat: Inscrito
Horario de silencio: 22:00 - 07:00
```

### Quiet Hours

Configure quiet hours to avoid notifications during specific times:

```
/silencio 22:00 07:00   # Set quiet hours (10 PM to 7 AM)
/silencio off           # Disable quiet hours
/silencio               # Show current setting
```

During quiet hours, notifications are queued. When the quiet period ends, you receive a summary of all events.

## Backup and Restore

The bot includes a backup system to protect against data loss. Only users listed in `ADMIN_USER_IDS` can use these features.

### Manual Backup

```
/backup
```

The bot will send a JSON file containing all subscribed chats with their settings.

### Restore from Backup

```
/restore         # Merge mode (keeps existing data)
/restore replace # Replace mode (clears existing data first)
```

Then send the JSON backup file to the bot.

### Automatic Backup

Configure automatic daily backups:

```bash
# Set your user ID as admin
./setup.sh config --admin YOUR_USER_ID

# Enable auto backup to a specific chat
./setup.sh config --backup-chat YOUR_CHAT_ID
```

Backups are sent daily at 3 AM.

### Getting IDs

- **Your user ID**: Message [@userinfobot](https://t.me/userinfobot) on Telegram
- **Chat ID**: Add the bot to the chat and use `/status`, or forward a message from the chat to @userinfobot

## Configuration

All configuration is done via environment variables in `.env`:

| Variable              | Default                      | Description                                    |
|-----------------------|------------------------------|------------------------------------------------|
| `TELEGRAM_BOT_TOKEN`  | *required*                   | Your Telegram bot token from @BotFather        |
| `RSS_FEED_URL`        | `https://status.ix.br/rss`   | IX.br status RSS feed URL                      |
| `CHECK_INTERVAL`      | `300` (5 minutes)            | Interval between RSS checks (min: 60 seconds)  |
| `MAX_MESSAGE_AGE_DAYS`| `7`                          | Maximum age for events (1-30 days)             |
| `DATABASE_PATH`       | `/app/data/ixbr_bot.db`      | Path to SQLite database file                   |
| `HEALTH_CHECK_FILE`   | `/app/data/health`           | Path to health check heartbeat file            |
| `LOG_LEVEL`           | `INFO`                       | Logging level (DEBUG, INFO, WARNING, ERROR)    |
| `RATE_LIMIT_COMMANDS` | `10`                         | Max commands per minute per chat               |
| `QUIET_HOURS_START`   | *optional*                   | Quiet hours start (HH:MM format)               |
| `QUIET_HOURS_END`     | *optional*                   | Quiet hours end (HH:MM format)                 |
| `ADMIN_USER_IDS`      | *optional*                   | Comma-separated admin Telegram user IDs        |
| `BACKUP_ENABLED`      | `false`                      | Enable automatic daily backups                 |
| `BACKUP_CHAT_ID`      | *optional*                   | Chat ID to send automatic backups to           |

## Project Structure

```
ixbrbot/
├── docker-compose.yml     # Docker Compose configuration
├── Dockerfile             # Docker image with health check
├── requirements.txt       # Python dependencies
├── setup.sh               # Management script
├── .env.example           # Environment variables template
├── .gitignore             # Git ignore rules
├── LICENSE                # MIT License
├── README.md              # This file
├── data/                  # SQLite database (gitignored)
└── src/
    ├── __init__.py        # Package initialization
    ├── bot.py             # Main bot with signal handling
    ├── config.py          # Pydantic settings + JSON logging
    ├── database.py        # Async SQLite (aiosqlite)
    └── rss_monitor.py     # RSS with retry logic (tenacity)
```

## Technical Details

### Resilience

- **RSS Retry**: Automatic retry with exponential backoff (3 attempts: 4s, 8s, 16s delays)
- **Graceful Shutdown**: Handles SIGTERM/SIGINT for clean Docker stops
- **Health Check**: Docker monitors a heartbeat file updated every 30 seconds
- **Rate Limiting**: Prevents command spam (configurable limit per chat)

### Logging

Logs are output in JSON format for easy parsing:

```json
{"timestamp": "2025-12-30 15:30:45", "level": "INFO", "logger": "ixbr_bot", "message": "Message sent", "chat_id": 123456, "event_guid": "abc123"}
```

### Database

Async SQLite with the following tables:

- `subscribed_chats` - Active subscriptions with quiet hours settings
- `sent_messages` - Tracking for duplicate prevention and message editing
- `command_log` - Rate limiting tracking
- `pending_notifications` - Queued notifications during quiet hours

## Message Format

```
[INCIDENTE]

IX.br Curitiba, PR - Indisponibilidade de PIX

Local: Curitiba, PR

Um rompimento de fibras opticas esta causando a indisponibilidade 
do PIX Cirion no IX.br Curitiba, PR.

Detalhes: https://status.ix.br/incident/1234/

Postado em: 29/12/2025 as 14:30 (UTC)
```

When an event is updated, the original message is edited with `[Mensagem atualizada]` note.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [IX.br](https://ix.br) - Brazilian Internet Exchange Point
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram Bot API wrapper
- [aiosqlite](https://github.com/omnilib/aiosqlite) - Async SQLite
- [tenacity](https://github.com/jd/tenacity) - Retry library
- [Pydantic](https://pydantic.dev/) - Configuration validation
- The Brazilian networking community

## Support

- For IX.br issues, contact: +55 11 5509-3550 or [Meu IX.br](https://meu.ix.br)
- For bot issues, open an issue on GitHub

---

**Made for the Brazilian networking community**
