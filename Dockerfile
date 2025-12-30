# IX.br Status Bot - Telegram Bot
# Monitors IX.br status page and sends updates to subscribed chats

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Create data directory for SQLite database and health check
RUN mkdir -p /app/data

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash botuser && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Health check - verifies the bot is running by checking heartbeat file
# The bot writes a timestamp to this file every 30 seconds
# If the file is older than 60 seconds, the container is unhealthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "from pathlib import Path; from datetime import datetime, timedelta; \
        f = Path('/app/data/health'); \
        exit(0 if f.exists() and datetime.now() - datetime.fromisoformat(f.read_text().strip()) < timedelta(seconds=60) else 1)"

# Run the bot
CMD ["python", "-m", "src.bot"]
