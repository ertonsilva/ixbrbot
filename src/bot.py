"""
Main bot module for IX.br Status Bot.
Handles Telegram bot interactions, RSS monitoring, and lifecycle management.
"""

import asyncio
import io
import json
import signal
from datetime import datetime, time, timedelta, timezone as tz
from pathlib import Path
from typing import Optional

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError

from .config import config, logger
from .database import Database
from .rss_monitor import RSSMonitor, StatusEvent

# Common timezones for Brazil
TIMEZONES = {
    "UTC": 0,
    "BRT": -3,      # Brasilia Time (most of Brazil)
    "AMT": -4,      # Amazon Time
    "ACT": -5,      # Acre Time
    "FNT": -2,      # Fernando de Noronha Time
}


class IXBRBot:
    """Main bot class handling Telegram interactions and RSS monitoring."""

    def __init__(self):
        """Initialize the bot with database and RSS monitor."""
        # Initialize components
        self.db = Database()
        self.rss_monitor = RSSMonitor()
        self._shutdown_event = asyncio.Event()
        self._health_check_task: Optional[asyncio.Task] = None

        # Build the Telegram application
        self.app = (
            Application.builder()
            .token(config.telegram_bot_token)
            .build()
        )

        # Register command handlers
        self._register_handlers()

        logger.info("Bot initialized", extra={
            "check_interval": config.check_interval,
            "max_message_age_days": config.max_message_age_days
        })

    def _register_handlers(self) -> None:
        """Register all command handlers."""
        handlers = [
            CommandHandler("start", self.cmd_start),
            CommandHandler("stop", self.cmd_stop),
            CommandHandler("status", self.cmd_status),
            CommandHandler("help", self.cmd_help),
            CommandHandler("silencio", self.cmd_quiet_hours),
            # Admin commands (not visible to users, only admins)
            CommandHandler("backup", self.cmd_backup),
            CommandHandler("restore", self.cmd_restore),
            CommandHandler("stats", self.cmd_stats),
        ]

        for handler in handlers:
            self.app.add_handler(handler)

        # Callback handler for inline keyboard menus
        self.app.add_handler(
            CallbackQueryHandler(self.callback_quiet_hours, pattern=r"^quiet:")
        )

        # Handler for receiving backup files
        self.app.add_handler(
            MessageHandler(
                filters.Document.MimeType("application/json"),
                self.handle_backup_file
            )
        )

        self.app.add_error_handler(self.error_handler)

    async def setup_commands(self) -> None:
        """Set up bot commands visible in Telegram."""
        # Public commands
        public_commands = [
            BotCommand("start", "Iniciar recebimento de notificacoes"),
            BotCommand("stop", "Parar de receber notificacoes"),
            BotCommand("status", "Verificar status do bot e do feed"),
            BotCommand("silencio", "Configurar horario de silencio"),
            BotCommand("help", "Mostrar ajuda e informacoes"),
        ]

        await self.app.bot.set_my_commands(public_commands)
        logger.info("Bot commands configured")

    # ==================== Permission Checks ====================

    async def _is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        """
        Check if a user is an administrator of the chat.
        Returns True for private chats (user is always "admin" of their own chat).
        """
        try:
            chat = await self.app.bot.get_chat(chat_id)

            # Private chats - user is always allowed
            if chat.type == ChatType.PRIVATE:
                return True

            # For groups/supergroups/channels, check admin status
            member = await self.app.bot.get_chat_member(chat_id, user_id)
            return member.status in ["creator", "administrator"]

        except TelegramError as e:
            logger.warning("Could not check admin status", extra={
                "chat_id": chat_id,
                "user_id": user_id,
                "error": str(e)
            })
            # Default to False for safety
            return False

    # ==================== Rate Limiting ====================

    async def _check_rate_limit(self, chat_id: int, command: str) -> bool:
        """
        Check if a command should be rate limited.
        Returns True if command is allowed, False if rate limited.
        """
        count = await self.db.get_command_count(chat_id, seconds=60)

        if count >= config.rate_limit_commands:
            logger.warning("Rate limit exceeded", extra={
                "chat_id": chat_id,
                "command": command,
                "count": count
            })
            return False

        await self.db.log_command(chat_id, command)
        return True

    # ==================== Quiet Hours ====================

    def _is_quiet_hours(
        self,
        quiet_start: Optional[str],
        quiet_end: Optional[str],
        quiet_tz: Optional[str] = "UTC"
    ) -> bool:
        """
        Check if current time is within quiet hours.

        Args:
            quiet_start: Start time in HH:MM format
            quiet_end: End time in HH:MM format
            quiet_tz: Timezone string (e.g., "BRT", "UTC")

        Returns:
            True if currently in quiet hours
        """
        if not quiet_start or not quiet_end:
            return False

        # Get UTC offset from timezone
        tz_offset = TIMEZONES.get(quiet_tz or "UTC", 0)

        # Get current time in UTC, then apply offset
        now_utc = datetime.now(tz.utc)
        offset_delta = now_utc + timedelta(hours=tz_offset)
        now = offset_delta.time()

        start_parts = quiet_start.split(":")
        end_parts = quiet_end.split(":")

        start = time(int(start_parts[0]), int(start_parts[1]))
        end = time(int(end_parts[0]), int(end_parts[1]))

        # Handle overnight quiet hours (e.g., 22:00 to 07:00)
        if start > end:
            return now >= start or now <= end
        else:
            return start <= now <= end

    # ==================== Command Handlers ====================

    async def cmd_start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start command - Subscribe chat to receive updates."""
        chat = update.effective_chat
        user = update.effective_user

        if not chat or not user:
            return

        # In groups/channels, only admins can subscribe
        if chat.type != ChatType.PRIVATE:
            if not await self._is_chat_admin(chat.id, user.id):
                await update.message.reply_text(
                    "Apenas administradores podem ativar notificacoes neste chat."
                )
                return

        # Rate limiting
        if not await self._check_rate_limit(chat.id, "start"):
            await update.message.reply_text(
                "Muitos comandos em pouco tempo. Aguarde um momento."
            )
            return

        chat_title = chat.title or chat.full_name or f"Chat {chat.id}"

        is_new = await self.db.subscribe_chat(
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat_title
        )

        if is_new:
            message = (
                "<b>Inscricao ativada!</b>\n\n"
                "Este chat agora recebera notificacoes de:\n"
                "- Incidentes e problemas\n"
                "- Janelas de manutencao programadas\n"
                "- Resolucoes de problemas\n\n"
                "Fonte: <a href=\"https://status.ix.br\">status.ix.br</a>\n\n"
                "Use /silencio para configurar horario de silencio.\n"
                "Use /stop para desativar as notificacoes.\n"
                "Use /help para mais informacoes."
            )
        else:
            message = (
                "Este chat ja esta inscrito para receber notificacoes.\n\n"
                "Use /stop para desativar as notificacoes.\n"
                "Use /help para mais informacoes."
            )

        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

        logger.info("Start command", extra={
            "user": user.username if user else "unknown",
            "chat_id": chat.id,
            "chat_type": chat.type,
            "is_new": is_new
        })

    async def cmd_stop(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop command - Unsubscribe chat from updates."""
        chat = update.effective_chat
        user = update.effective_user

        if not chat or not user:
            return

        # In groups/channels, only admins can unsubscribe
        if chat.type != ChatType.PRIVATE:
            if not await self._is_chat_admin(chat.id, user.id):
                await update.message.reply_text(
                    "Apenas administradores podem desativar notificacoes neste chat."
                )
                return

        if not await self._check_rate_limit(chat.id, "stop"):
            await update.message.reply_text(
                "Muitos comandos em pouco tempo. Aguarde um momento."
            )
            return

        was_subscribed = await self.db.unsubscribe_chat(chat.id)

        if was_subscribed:
            message = (
                "<b>Inscricao desativada!</b>\n\n"
                "Este chat nao recebera mais notificacoes do IX.br.\n\n"
                "Use /start para reativar as notificacoes."
            )
        else:
            message = (
                "Este chat nao estava inscrito.\n\n"
                "Use /start para ativar as notificacoes."
            )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)

        logger.info("Stop command", extra={
            "user": user.username if user else "unknown",
            "chat_id": chat.id,
            "was_subscribed": was_subscribed
        })

    async def cmd_status(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status command - Show bot and feed status."""
        chat = update.effective_chat

        if not chat:
            return

        if not await self._check_rate_limit(chat.id, "status"):
            await update.message.reply_text(
                "Muitos comandos em pouco tempo. Aguarde um momento."
            )
            return

        checking_msg = await update.message.reply_text("Verificando status...")

        feed_status = self.rss_monitor.check_feed_status()

        lines = ["<b>Status do Bot</b>", ""]
        lines.append("Bot: <b>Online</b>")

        if feed_status["reachable"]:
            lines.append("Feed RSS (status.ix.br): <b>Acessivel</b>")
        else:
            lines.append("Feed RSS (status.ix.br): <b>Inacessivel</b>")
            if feed_status["error"]:
                lines.append(f"  Erro: {feed_status['error'][:100]}")

        lines.append("")

        if feed_status["last_post_date"]:
            date_str = feed_status["last_post_date"].strftime("%d/%m/%Y as %H:%M")
            lines.append(f"<b>Ultimo post:</b> {date_str}")
            if feed_status["last_post_title"]:
                title = feed_status["last_post_title"]
                if len(title) > 60:
                    title = title[:60] + "..."
                lines.append(f"<i>{title}</i>")
        else:
            lines.append("Ultimo post: <i>Nao disponivel</i>")

        lines.append("")

        is_subscribed = await self.db.is_chat_subscribed(chat.id)
        sub_status = "Inscrito" if is_subscribed else "Nao inscrito"
        lines.append(f"Este chat: <b>{sub_status}</b>")

        # Show quiet hours if configured
        quiet = await self.db.get_chat_quiet_hours(chat.id)
        if quiet:
            tz_info = f" ({quiet[2]})" if len(quiet) > 2 and quiet[2] else ""
            lines.append(f"Horario de silencio: {quiet[0]} - {quiet[1]}{tz_info}")

        await checking_msg.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML
        )

    async def cmd_quiet_hours(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle /silencio command - Configure quiet hours with interactive menu.
        Usage: /silencio              (show menu)
               /silencio 22:00 07:00  (set directly, assumes UTC)
               /silencio BRT 22:00 07:00  (set with timezone)
               /silencio off          (disable)
        """
        chat = update.effective_chat

        if not chat:
            return

        if not await self._check_rate_limit(chat.id, "silencio"):
            await update.message.reply_text(
                "Muitos comandos em pouco tempo. Aguarde um momento."
            )
            return

        args = context.args or []

        # No args - show interactive menu
        if len(args) == 0:
            quiet = await self.db.get_chat_quiet_hours(chat.id)

            if quiet:
                current_text = (
                    f"<b>Configuracao atual:</b>\n"
                    f"Inicio: {quiet[0]} | Fim: {quiet[1]}\n"
                    f"Timezone: {quiet[2] if len(quiet) > 2 and quiet[2] else 'UTC'}\n\n"
                )
            else:
                current_text = "<b>Horario de silencio: Desativado</b>\n\n"

            keyboard = [
                [
                    InlineKeyboardButton("22:00 - 07:00 (BRT)", callback_data="quiet:BRT:22:00:07:00"),
                    InlineKeyboardButton("23:00 - 08:00 (BRT)", callback_data="quiet:BRT:23:00:08:00"),
                ],
                [
                    InlineKeyboardButton("00:00 - 06:00 (BRT)", callback_data="quiet:BRT:00:00:06:00"),
                    InlineKeyboardButton("21:00 - 06:00 (BRT)", callback_data="quiet:BRT:21:00:06:00"),
                ],
                [
                    InlineKeyboardButton("Selecionar timezone", callback_data="quiet:tz"),
                ],
                [
                    InlineKeyboardButton("Desativar", callback_data="quiet:off"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                current_text +
                "Selecione uma opcao ou configure manualmente:\n"
                "<code>/silencio HH:MM HH:MM</code> (UTC)\n"
                "<code>/silencio BRT 22:00 07:00</code> (com timezone)",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return

        # Disable quiet hours
        if args[0].lower() == "off":
            await self.db.set_quiet_hours(chat.id, None, None, None)
            await update.message.reply_text(
                "Horario de silencio desativado.\n"
                "Notificacoes serao enviadas imediatamente."
            )
            logger.info("Quiet hours disabled", extra={"chat_id": chat.id})
            return

        # Parse arguments: could be "HH:MM HH:MM" or "TZ HH:MM HH:MM"
        timezone_str = "UTC"
        if len(args) == 2:
            start, end = args[0], args[1]
        elif len(args) == 3:
            timezone_str = args[0].upper()
            if timezone_str not in TIMEZONES:
                await update.message.reply_text(
                    f"Timezone invalido: {timezone_str}\n"
                    f"Opcoes validas: {', '.join(TIMEZONES.keys())}"
                )
                return
            start, end = args[1], args[2]
        else:
            await update.message.reply_text(
                "Uso:\n"
                "/silencio HH:MM HH:MM (UTC)\n"
                "/silencio BRT 22:00 07:00 (com timezone)\n\n"
                "Timezones: " + ", ".join(TIMEZONES.keys())
            )
            return

        # Validate time format
        try:
            for t in [start, end]:
                parts = t.split(":")
                if len(parts) != 2:
                    raise ValueError()
                h, m = int(parts[0]), int(parts[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "Formato invalido. Use HH:MM (ex: 22:00)"
            )
            return

        await self.db.set_quiet_hours(chat.id, start, end, timezone_str)
        await update.message.reply_text(
            f"Horario de silencio configurado!\n\n"
            f"Inicio: {start}\n"
            f"Fim: {end}\n"
            f"Timezone: {timezone_str}\n\n"
            f"Durante este periodo, notificacoes serao acumuladas e "
            f"enviadas em resumo quando o silencio terminar."
        )

        logger.info("Quiet hours set", extra={
            "chat_id": chat.id,
            "start": start,
            "end": end,
            "timezone": timezone_str
        })

    async def callback_quiet_hours(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle quiet hours menu callbacks."""
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()

        chat_id = query.message.chat_id
        data = query.data

        if data == "quiet:off":
            await self.db.set_quiet_hours(chat_id, None, None, None)
            await query.edit_message_text(
                "Horario de silencio desativado.\n"
                "Notificacoes serao enviadas imediatamente."
            )
            logger.info("Quiet hours disabled via menu", extra={"chat_id": chat_id})
            return

        if data == "quiet:tz":
            # Show timezone selection
            keyboard = [
                [InlineKeyboardButton(f"{tz} (UTC{'+' if TIMEZONES[tz] >= 0 else ''}{TIMEZONES[tz]})",
                                      callback_data=f"quiet:tzsel:{tz}")]
                for tz in TIMEZONES.keys()
            ]
            keyboard.append([InlineKeyboardButton("Voltar", callback_data="quiet:back")])

            await query.edit_message_text(
                "<b>Selecione o timezone:</b>\n\n"
                "BRT = Brasilia (a maioria do Brasil)\n"
                "AMT = Amazonas\n"
                "ACT = Acre\n"
                "FNT = Fernando de Noronha",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        if data.startswith("quiet:tzsel:"):
            # Store selected timezone, show time selection
            timezone_str = data.split(":")[2]
            context.user_data["quiet_tz"] = timezone_str

            keyboard = [
                [
                    InlineKeyboardButton("22:00 - 07:00", callback_data=f"quiet:set:{timezone_str}:22:00:07:00"),
                    InlineKeyboardButton("23:00 - 08:00", callback_data=f"quiet:set:{timezone_str}:23:00:08:00"),
                ],
                [
                    InlineKeyboardButton("00:00 - 06:00", callback_data=f"quiet:set:{timezone_str}:00:00:06:00"),
                    InlineKeyboardButton("21:00 - 06:00", callback_data=f"quiet:set:{timezone_str}:21:00:06:00"),
                ],
                [InlineKeyboardButton("Voltar", callback_data="quiet:tz")],
            ]

            await query.edit_message_text(
                f"<b>Timezone: {timezone_str}</b>\n\n"
                f"Selecione o horario de silencio:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        if data == "quiet:back":
            # Back to main menu - re-invoke command
            quiet = await self.db.get_chat_quiet_hours(chat_id)

            if quiet:
                current_text = (
                    f"<b>Configuracao atual:</b>\n"
                    f"Inicio: {quiet[0]} | Fim: {quiet[1]}\n"
                    f"Timezone: {quiet[2] if len(quiet) > 2 and quiet[2] else 'UTC'}\n\n"
                )
            else:
                current_text = "<b>Horario de silencio: Desativado</b>\n\n"

            keyboard = [
                [
                    InlineKeyboardButton("22:00 - 07:00 (BRT)", callback_data="quiet:BRT:22:00:07:00"),
                    InlineKeyboardButton("23:00 - 08:00 (BRT)", callback_data="quiet:BRT:23:00:08:00"),
                ],
                [
                    InlineKeyboardButton("00:00 - 06:00 (BRT)", callback_data="quiet:BRT:00:00:06:00"),
                    InlineKeyboardButton("21:00 - 06:00 (BRT)", callback_data="quiet:BRT:21:00:06:00"),
                ],
                [InlineKeyboardButton("Selecionar timezone", callback_data="quiet:tz")],
                [InlineKeyboardButton("Desativar", callback_data="quiet:off")],
            ]

            await query.edit_message_text(
                current_text +
                "Selecione uma opcao ou configure manualmente:\n"
                "<code>/silencio HH:MM HH:MM</code> (UTC)\n"
                "<code>/silencio BRT 22:00 07:00</code> (com timezone)",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # Handle direct selection: quiet:TZ:HH:MM:HH:MM or quiet:set:TZ:HH:MM:HH:MM
        parts = data.split(":")
        if len(parts) >= 5:
            if parts[1] == "set":
                timezone_str, start, end = parts[2], f"{parts[3]}:{parts[4]}", f"{parts[5]}:{parts[6]}"
            else:
                timezone_str, start, end = parts[1], f"{parts[2]}:{parts[3]}", f"{parts[4]}:{parts[5]}"

            await self.db.set_quiet_hours(chat_id, start, end, timezone_str)
            await query.edit_message_text(
                f"<b>Horario de silencio configurado!</b>\n\n"
                f"Inicio: {start}\n"
                f"Fim: {end}\n"
                f"Timezone: {timezone_str}\n\n"
                f"Durante este periodo, notificacoes serao acumuladas e "
                f"enviadas em resumo quando o silencio terminar.",
                parse_mode=ParseMode.HTML
            )
            logger.info("Quiet hours set via menu", extra={
                "chat_id": chat_id,
                "start": start,
                "end": end,
                "timezone": timezone_str
            })

    async def cmd_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help command - Show help information."""
        chat = update.effective_chat
        user = update.effective_user

        if not chat:
            return

        if not await self._check_rate_limit(chat.id, "help"):
            return

        message = (
            "<b>IX.br Status Bot</b>\n\n"
            "Este bot envia notificacoes sobre o status do IX.br "
            "(PTT - Ponto de Troca de Trafego brasileiro).\n\n"
            "<b>Comandos disponiveis:</b>\n"
            "/start - Ativar notificacoes neste chat\n"
            "/stop - Desativar notificacoes\n"
            "/status - Verificar status do bot e do feed\n"
            "/silencio - Configurar horario de silencio\n"
            "/help - Mostrar esta mensagem\n\n"
            "<b>Tipos de notificacoes:</b>\n"
            "- Incidentes e problemas\n"
            "- Manutencoes programadas\n"
            "- Problemas resolvidos\n\n"
            "<b>Horario de silencio:</b>\n"
            "Use /silencio 22:00 07:00 para nao receber "
            "notificacoes durante a noite.\n\n"
            "<b>Links uteis:</b>\n"
            "- Pagina de Status: https://status.ix.br\n"
            "- Site oficial: https://ix.br\n\n"
            "<i>Desenvolvido para a comunidade de redes brasileira</i>"
        )

        # Add admin commands info for admins
        if user and config.is_admin(user.id):
            message += (
                "\n\n<b>Comandos de admin:</b>\n"
                "/backup - Exportar backup dos chats\n"
                "/restore - Restaurar backup (envie o arquivo)\n"
                "/stats - Estatisticas detalhadas"
            )

        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

    # ==================== Admin Commands ====================

    async def cmd_backup(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /backup command - Export subscriptions (admin only)."""
        user = update.effective_user
        chat = update.effective_chat

        if not user or not chat:
            return

        if not config.is_admin(user.id):
            await update.message.reply_text(
                "Este comando e restrito a administradores."
            )
            logger.warning("Unauthorized backup attempt", extra={
                "user_id": user.id,
                "username": user.username
            })
            return

        await update.message.reply_text("Gerando backup...")

        try:
            # Export data
            backup_data = await self.db.export_backup()

            # Create JSON file
            json_str = json.dumps(backup_data, indent=2, ensure_ascii=False)
            json_bytes = json_str.encode("utf-8")

            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"ixbr_bot_backup_{timestamp}.json"

            # Send as document
            await update.message.reply_document(
                document=io.BytesIO(json_bytes),
                filename=filename,
                caption=(
                    f"Backup realizado com sucesso!\n\n"
                    f"Chats ativos: {backup_data['stats']['active_chats']}\n"
                    f"Total de chats: {len(backup_data['subscribed_chats'])}\n\n"
                    f"Para restaurar, use /restore e envie este arquivo."
                )
            )

            logger.info("Backup created and sent", extra={
                "admin_id": user.id,
                "chats_count": len(backup_data["subscribed_chats"])
            })

        except Exception as e:
            logger.error("Backup failed", extra={
                "error": str(e),
                "admin_id": user.id
            })
            await update.message.reply_text(
                f"Erro ao gerar backup: {str(e)}"
            )

    async def cmd_restore(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /restore command - Show restore instructions (admin only)."""
        user = update.effective_user

        if not user:
            return

        if not config.is_admin(user.id):
            await update.message.reply_text(
                "Este comando e restrito a administradores."
            )
            return

        # Check for merge flag
        args = context.args or []
        if args and args[0] == "replace":
            context.user_data["restore_mode"] = "replace"
            mode_text = "SUBSTITUIR (apaga dados existentes)"
        else:
            context.user_data["restore_mode"] = "merge"
            mode_text = "MESCLAR (mantem dados existentes)"

        await update.message.reply_text(
            f"<b>Restauracao de Backup</b>\n\n"
            f"Modo atual: <b>{mode_text}</b>\n\n"
            f"Envie o arquivo JSON de backup para restaurar.\n\n"
            f"Opcoes:\n"
            f"/restore - Mescla com dados existentes\n"
            f"/restore replace - Substitui todos os dados",
            parse_mode=ParseMode.HTML
        )

    async def handle_backup_file(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle backup file upload for restore."""
        user = update.effective_user
        message = update.message

        if not user or not message or not message.document:
            return

        if not config.is_admin(user.id):
            # Ignore files from non-admins
            return

        # Get restore mode
        merge = context.user_data.get("restore_mode", "merge") == "merge"

        # Check file size
        if message.document.file_size > config.max_backup_size:
            max_mb = config.max_backup_size / 1024 / 1024
            await message.reply_text(
                f"Arquivo muito grande. Maximo permitido: {max_mb:.1f}MB"
            )
            logger.warning("Backup file too large", extra={
                "user_id": user.id,
                "file_size": message.document.file_size
            })
            return

        await message.reply_text("Processando arquivo de backup...")

        try:
            # Download file
            file = await message.document.get_file()
            file_bytes = await file.download_as_bytearray()

            # Parse JSON
            backup_data = json.loads(file_bytes.decode("utf-8"))

            # Validate
            if "subscribed_chats" not in backup_data:
                await message.reply_text(
                    "Arquivo invalido: nao contem dados de chats."
                )
                return

            # Confirm before restore
            chats_count = len(backup_data["subscribed_chats"])
            mode_text = "mesclar" if merge else "substituir"

            # Do the import
            result = await self.db.import_backup(backup_data, merge=merge)

            await message.reply_text(
                f"<b>Restauracao concluida!</b>\n\n"
                f"Modo: {mode_text}\n"
                f"Chats no backup: {result['total_in_backup']}\n"
                f"Importados: {result['imported']}\n"
                f"Ignorados (ja existiam): {result['skipped']}\n"
                f"Erros: {result['errors']}",
                parse_mode=ParseMode.HTML
            )

            logger.info("Backup restored", extra={
                "admin_id": user.id,
                "result": result,
                "merge": merge
            })

        except json.JSONDecodeError:
            await message.reply_text("Erro: arquivo JSON invalido.")
        except Exception as e:
            logger.error("Restore failed", extra={
                "error": str(e),
                "admin_id": user.id
            })
            await message.reply_text(f"Erro ao restaurar: {str(e)}")

    async def cmd_stats(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stats command - Show detailed statistics (admin only)."""
        user = update.effective_user
        chat = update.effective_chat

        if not user or not chat:
            return

        if not config.is_admin(user.id):
            await update.message.reply_text(
                "Este comando e restrito a administradores."
            )
            return

        stats = await self.db.get_stats()
        feed_status = self.rss_monitor.check_feed_status()

        lines = [
            "<b>Estatisticas do Bot</b>",
            "",
            f"<b>Chats:</b>",
            f"  Ativos: {stats['active_chats']}",
            "",
            f"<b>Mensagens:</b>",
            f"  Total enviadas: {stats['total_messages_sent']}",
            f"  Falhas de entrega: {stats['failed_deliveries']}",
            "",
            f"<b>RSS Feed:</b>",
            f"  Status: {'Acessivel' if feed_status['reachable'] else 'Inacessivel'}",
            f"  Entradas no feed: {feed_status.get('total_entries', 'N/A')}",
            f"  Falhas consecutivas: {feed_status.get('consecutive_failures', 0)}",
            "",
            f"<b>Configuracao:</b>",
            f"  Intervalo de check: {config.check_interval}s",
            f"  Idade max eventos: {config.max_message_age_days} dias",
            f"  Rate limit: {config.rate_limit_commands}/min",
            f"  Admins: {len(config.get_admin_ids())}",
        ]

        if feed_status.get("last_post_date"):
            date_str = feed_status["last_post_date"].strftime("%d/%m/%Y %H:%M")
            lines.append(f"\n<b>Ultimo post:</b> {date_str}")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML
        )

    async def error_handler(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle errors during bot operation."""
        logger.error("Bot error", extra={
            "error": str(context.error),
            "error_type": type(context.error).__name__
        }, exc_info=context.error)

    # ==================== RSS Monitoring ====================

    async def check_rss_updates(
        self,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None
    ) -> None:
        """Check RSS feed for new updates and send to subscribed chats."""
        logger.debug("Checking RSS feed for updates")

        try:
            events = await self.rss_monitor.fetch_events()

            if not events:
                logger.debug("No events found in RSS feed")
                return

            active_chats = await self.db.get_active_chats()

            if not active_chats:
                logger.debug("No active chats to send updates to")
                return

            for event in events:
                await self._send_event_to_chats(event, active_chats)

            # Send pending notifications for chats exiting quiet hours
            await self._process_pending_notifications(active_chats)

            # Cleanup
            await self.db.cleanup_old_messages()
            await self.db.cleanup_command_log()

        except Exception as e:
            logger.error("Error checking RSS updates", extra={
                "error": str(e),
                "error_type": type(e).__name__
            })

    async def _send_event_to_chats(
        self,
        event: StatusEvent,
        chats: list[dict]
    ) -> None:
        """Send or update an event to all specified chats."""
        message_text = event.to_telegram_message()
        current_hash = event.get_content_hash()

        for chat_info in chats:
            chat_id = chat_info["chat_id"]

            # Check quiet hours
            if self._is_quiet_hours(
                chat_info.get("quiet_hours_start"),
                chat_info.get("quiet_hours_end"),
                chat_info.get("quiet_hours_tz")
            ):
                # Store for later
                await self.db.add_pending_notification(
                    chat_id=chat_id,
                    message_guid=event.guid,
                    message_text=message_text,
                    event_title=event.title
                )
                logger.debug("Notification queued (quiet hours)", extra={
                    "chat_id": chat_id,
                    "event_guid": event.guid
                })
                continue

            # Check if message was already sent
            existing = await self.db.get_sent_message(event.guid, chat_id)

            if existing:
                if existing["content_hash"] == current_hash:
                    continue

                # Content changed - try to edit
                telegram_msg_id = existing.get("telegram_message_id")
                if telegram_msg_id:
                    try:
                        updated_text = f"{message_text}\n\n<i>[Mensagem atualizada]</i>"

                        await self.app.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=telegram_msg_id,
                            text=updated_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=False
                        )

                        await self.db.update_message_record(
                            message_guid=event.guid,
                            chat_id=chat_id,
                            content_hash=current_hash,
                            message_title=event.title
                        )

                        logger.info("Message updated", extra={
                            "chat_id": chat_id,
                            "event_guid": event.guid,
                            "telegram_message_id": telegram_msg_id
                        })

                        await asyncio.sleep(0.1)
                        continue

                    except TelegramError as e:
                        logger.warning("Could not edit message", extra={
                            "chat_id": chat_id,
                            "error": str(e)
                        })

            # Send new message
            await self._send_message(chat_id, event, message_text, current_hash)

    async def _send_message(
        self,
        chat_id: int,
        event: StatusEvent,
        message_text: str,
        content_hash: str
    ) -> bool:
        """Send a message and handle delivery status."""
        try:
            sent_message = await self.app.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )

            await self.db.mark_message_sent(
                message_guid=event.guid,
                chat_id=chat_id,
                telegram_message_id=sent_message.message_id,
                content_hash=content_hash,
                message_title=event.title,
                delivery_status="sent"
            )

            logger.info("Message sent", extra={
                "chat_id": chat_id,
                "event_guid": event.guid,
                "telegram_message_id": sent_message.message_id
            })

            await asyncio.sleep(0.1)
            return True

        except TelegramError as e:
            error_str = str(e).lower()

            # Determine if chat is permanently inaccessible
            permanent_errors = [
                "blocked", "deactivated", "not found",
                "chat not found", "kicked", "forbidden"
            ]

            if any(x in error_str for x in permanent_errors):
                logger.warning("Chat inaccessible, unsubscribing", extra={
                    "chat_id": chat_id,
                    "error": str(e)
                })
                await self.db.unsubscribe_chat(chat_id)
            else:
                # Temporary error - log for tracking
                logger.error("Message delivery failed", extra={
                    "chat_id": chat_id,
                    "event_guid": event.guid,
                    "error": str(e),
                    "error_type": type(e).__name__
                })

            return False

    async def _process_pending_notifications(
        self,
        chats: list[dict]
    ) -> None:
        """Send pending notifications for chats that exited quiet hours."""
        for chat_info in chats:
            chat_id = chat_info["chat_id"]

            # Skip if still in quiet hours
            if self._is_quiet_hours(
                chat_info.get("quiet_hours_start"),
                chat_info.get("quiet_hours_end"),
                chat_info.get("quiet_hours_tz")
            ):
                continue

            pending = await self.db.get_pending_notifications(chat_id)
            if not pending:
                continue

            try:
                # Send notifications
                if len(pending) == 1:
                    # Just send the single notification
                    sent_msg = await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=pending[0]["message_text"],
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )
                    # Mark as sent in sent_messages to prevent resend
                    await self.db.mark_message_sent(
                        message_guid=pending[0]["message_guid"],
                        chat_id=chat_id,
                        telegram_message_id=sent_msg.message_id,
                        content_hash="pending_delivered",
                        message_title=pending[0]["event_title"],
                        delivery_status="sent"
                    )
                else:
                    # Send summary header
                    summary = (
                        f"<b>Resumo: {len(pending)} notificacoes durante silencio</b>\n\n"
                    )
                    for p in pending[:5]:  # Limit to 5 in summary
                        title = p["event_title"] or "Evento"
                        if len(title) > 50:
                            title = title[:50] + "..."
                        summary += f"- {title}\n"

                    if len(pending) > 5:
                        summary += f"\n... e mais {len(pending) - 5} eventos."

                    summary += "\n\nVeja detalhes em https://status.ix.br"

                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=summary,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    # Mark all as sent
                    for p in pending:
                        await self.db.mark_message_sent(
                            message_guid=p["message_guid"],
                            chat_id=chat_id,
                            telegram_message_id=0,  # Summary message
                            content_hash="pending_summary",
                            message_title=p["event_title"],
                            delivery_status="sent"
                        )
            except TelegramError as e:
                logger.error("Failed to send pending notifications", extra={
                    "chat_id": chat_id,
                    "error": str(e),
                    "pending_count": len(pending)
                })
                continue  # Don't clear if sending failed

            # Clear pending
            await self.db.clear_pending_notifications(chat_id)
            logger.info("Sent pending notifications", extra={
                "chat_id": chat_id,
                "count": len(pending)
            })

    # ==================== Health Check ====================

    async def _health_check_loop(self) -> None:
        """Periodically update health check file."""
        health_file = Path(config.health_check_file)

        while not self._shutdown_event.is_set():
            try:
                # Write current timestamp
                health_file.write_text(
                    datetime.now().isoformat()
                )
            except Exception as e:
                logger.error("Health check write failed", extra={
                    "error": str(e)
                })

            # Wait 30 seconds or until shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=30
                )
                break
            except asyncio.TimeoutError:
                pass

    # ==================== Auto Backup ====================

    async def _auto_backup(
        self,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None
    ) -> None:
        """Perform automatic backup and send to configured chat."""
        if not config.backup_enabled or not config.backup_chat_id:
            return

        try:
            backup_data = await self.db.export_backup()

            json_str = json.dumps(backup_data, indent=2, ensure_ascii=False)
            json_bytes = json_str.encode("utf-8")

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"ixbr_bot_backup_{timestamp}.json"

            await self.app.bot.send_document(
                chat_id=config.backup_chat_id,
                document=io.BytesIO(json_bytes),
                filename=filename,
                caption=(
                    f"Backup automatico\n\n"
                    f"Chats ativos: {backup_data['stats']['active_chats']}\n"
                    f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                )
            )

            logger.info("Auto backup completed", extra={
                "chats_count": len(backup_data["subscribed_chats"]),
                "backup_chat_id": config.backup_chat_id
            })

        except Exception as e:
            logger.error("Auto backup failed", extra={
                "error": str(e)
            })

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        """Start the bot with polling and RSS monitoring."""
        # Initialize database
        await self.db.init()
        config.ensure_data_directory()

        # Set up commands
        await self.setup_commands()

        # Start health check
        self._health_check_task = asyncio.create_task(
            self._health_check_loop()
        )

        # Schedule RSS monitoring
        job_queue: JobQueue = self.app.job_queue
        job_queue.run_repeating(
            callback=self.check_rss_updates,
            interval=config.check_interval,
            first=10
        )

        # Schedule auto backup (daily at 3 AM)
        if config.backup_enabled and config.backup_chat_id:
            job_queue.run_daily(
                callback=self._auto_backup,
                time=time(hour=3, minute=0),
                name="auto_backup"
            )
            logger.info("Auto backup scheduled", extra={
                "backup_chat_id": config.backup_chat_id
            })

        logger.info("Starting bot", extra={
            "check_interval": config.check_interval,
            "admins": len(config.get_admin_ids())
        })

        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

        logger.info("Bot is running")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("Stopping bot...")

        # Signal shutdown
        self._shutdown_event.set()

        # Cancel health check
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Stop Telegram (only if it was started)
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
                await self.app.shutdown()
        except Exception as e:
            logger.debug("Error during shutdown (may be normal if bot didn't fully start)", 
                        extra={"error": str(e)})

        logger.info("Bot stopped successfully")

    def signal_handler(self, sig: signal.Signals) -> None:
        """Handle shutdown signals (SIGTERM, SIGINT)."""
        logger.info("Received shutdown signal", extra={
            "signal": sig.name
        })
        self._shutdown_event.set()


async def main() -> None:
    """Main entry point for the bot."""
    bot = IXBRBot()

    # Set up signal handlers
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: bot.signal_handler(s)
        )

    try:
        await bot.start()
    except Exception as e:
        logger.error("Bot crashed", extra={
            "error": str(e),
            "error_type": type(e).__name__
        }, exc_info=True)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
