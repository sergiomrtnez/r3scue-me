#!/usr/bin/env python3
"""
telegram_listener.py - Lightweight Telegram Inbound Listener (The Producer).

Operates an infinite long-polling loop to provide instant UX feedback for
user messages and interactive inline menus.

STRICT ARCHITECTURAL CONSTRAINTS:
- ZERO AI imports (no llama.cpp, no AIHandler) to prevent RAM saturation.
- Purely handles inbound text ingest (saving to data/notes_inbox/) and UI queries.
- Memory footprint: ~15-20 MB.
"""

from datetime import datetime
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message
)

# Setup dedicated listener logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [TelegramListener]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TelegramListener")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INBOX_DIR = os.path.join(BASE_DIR, "data", "notes_inbox")
VAULT_DIR = os.path.join(BASE_DIR, "data", "notes_vault")


class TelegramListener:
    """
    Lightweight inbound Telegram listener and note ingest producer.
    Does not touch AIHandler or heavy libraries.
    """

    def __init__(
        self,
        bot_token: str,
        authorized_chat_id: str = "",
        inbox_dir: str = INBOX_DIR,
        vault_dir: str = VAULT_DIR
    ) -> None:
        self.bot_token = bot_token
        self.authorized_chat_id = str(authorized_chat_id).strip()
        self.inbox_dir = inbox_dir
        self.vault_dir = vault_dir

        os.makedirs(self.inbox_dir, exist_ok=True)
        os.makedirs(self.vault_dir, exist_ok=True)

        self.bot = telebot.TeleBot(self.bot_token, parse_mode=None)
        self._register_handlers()

    def is_authorized(self, chat_id: int) -> bool:
        """
        Security gate: verify sender matches authorized chat ID.
        """
        if not self.authorized_chat_id:
            return True
        return str(chat_id) == self.authorized_chat_id

    def build_main_keyboard(self) -> InlineKeyboardMarkup:
        """
        Construct interactive Inline Keyboard for all available automation modules.
        """
        markup = InlineKeyboardMarkup(row_width=2)
        btn_notes = InlineKeyboardButton("📝 Notas", callback_data="cmd_view_notes")
        btn_tasks = InlineKeyboardButton("📋 Tareas", callback_data="cmd_view_tasks")
        btn_news = InlineKeyboardButton("📰 Noticias", callback_data="cmd_view_news")
        btn_deals = InlineKeyboardButton("🔥 Chollos", callback_data="cmd_view_deals")
        btn_status = InlineKeyboardButton("⚡ Estado Servidor", callback_data="cmd_view_status")
        
        markup.add(btn_notes, btn_tasks)
        markup.add(btn_news, btn_deals)
        markup.add(btn_status)
        return markup

    def _register_handlers(self) -> None:
        """
        Register message and callback query handlers with TeleBot.
        """

        @self.bot.message_handler(commands=["start", "help"])
        def handle_start(message: Message) -> None:
            if not self.is_authorized(message.chat.id):
                logger.warning(f"Unauthorized access attempt from chat_id: {message.chat.id}")
                self.bot.reply_to(message, "⛔ Acceso denegado: este bot es de uso personal privado.")
                return

            welcome_text = (
                "🤖 *DroidServer-AI Control Center*\n\n"
                "¡Bienvenido! Tu servidor Android de automatizaciones está activo.\n\n"
                "• *Enviar Notas*: Escribe cualquier pensamiento o texto y lo guardaré al instante en el inbox.\n"
                "• *Panel de Control*: Usa los botones interactivos abajo para consultar cualquiera de tus módulos de IA."
            )
            self.bot.send_message(
                message.chat.id,
                welcome_text,
                parse_mode="Markdown",
                reply_markup=self.build_main_keyboard()
            )

        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_callbacks(call: CallbackQuery) -> None:
            if not self.is_authorized(call.message.chat.id):
                self.bot.answer_callback_query(call.id, "No autorizado.", show_alert=True)
                return

            data = call.data
            logger.info(f"Processing callback action: {data}")

            # 1. Modulo Smart Notes
            if data == "cmd_view_notes":
                # Check pending inbox notes
                inbox_files = [
                    f for f in os.listdir(self.inbox_dir)
                    if os.path.isfile(os.path.join(self.inbox_dir, f)) and not f.startswith(".")
                ]
                inbox_info = f"📥 *Pendientes en inbox:* {len(inbox_files)} notas (esperando próximo cron)\n\n"

                # Check organized vault files
                md_files = []
                for root, _, files in os.walk(self.vault_dir):
                    for file in files:
                        if file.endswith(".md"):
                            rel_path = os.path.relpath(os.path.join(root, file), self.vault_dir)
                            md_files.append(rel_path)

                if not md_files:
                    response_text = f"{inbox_info}📂 *Baúl Markdown:* Vacío por el momento."
                else:
                    sample = md_files[:15]
                    list_str = "\n".join(f"• `{f}`" for f in sample)
                    response_text = (
                        f"{inbox_info}📚 *Notas en el baúl ({len(md_files)} total):*\n\n{list_str}"
                    )
                    if len(md_files) > 15:
                        response_text += f"\n\n_...y {len(md_files) - 15} notas más._"

                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    response_text,
                    parse_mode="Markdown",
                    reply_markup=self.build_main_keyboard()
                )

            # 2. Modulo Task Reminder
            elif data == "cmd_view_tasks":
                tasks = []
                if os.path.isfile(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            fresh_cfg = json.load(f)
                            tasks = fresh_cfg.get("modules", {}).get("task_reminder", {}).get("tasks", [])
                    except Exception as e:
                        logger.error(f"Failed to load fresh tasks from config: {e}")

                if not tasks:
                    response_text = "📋 No hay tareas pendientes configuradas en `task_reminder`."
                else:
                    if isinstance(tasks, list):
                        formatted = "\n".join(f"• {t}" for t in tasks)
                    else:
                        formatted = str(tasks)
                    response_text = f"🎯 *Tareas Pendientes Registradas:*\n\n{formatted}"

                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    response_text,
                    parse_mode="Markdown",
                    reply_markup=self.build_main_keyboard()
                )

            # 3. Modulo News Summarizer
            elif data == "cmd_view_news":
                urls = []
                if os.path.isfile(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            fresh_cfg = json.load(f)
                            urls = fresh_cfg.get("modules", {}).get("news_summarizer", {}).get("urls", [])
                    except Exception as e:
                        logger.error(f"Failed to load news urls: {e}")

                if not urls:
                    response_text = "📰 No hay feeds o URLs configuradas en `news_summarizer`."
                else:
                    formatted_urls = "\n".join(f"• {u}" for u in urls)
                    response_text = f"📰 *Fuentes de Noticias Monitoreadas:*\n\n{formatted_urls}"

                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    response_text,
                    parse_mode="Markdown",
                    reply_markup=self.build_main_keyboard()
                )

            # 4. Modulo Deal Finder
            elif data == "cmd_view_deals":
                deal_cfg = {}
                if os.path.isfile(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            fresh_cfg = json.load(f)
                            deal_cfg = fresh_cfg.get("modules", {}).get("deal_finder", {})
                    except Exception as e:
                        logger.error(f"Failed to load deal finder cfg: {e}")

                keywords = deal_cfg.get("keywords", [])
                urls = deal_cfg.get("urls", [])

                kw_str = ", ".join(f"`{k}`" for k in keywords) if keywords else "_Ninguna_"
                url_str = "\n".join(f"• {u}" for u in urls) if urls else "_Ninguna_"

                response_text = (
                    f"🔥 *Configuración de Monitor de Chollos (DealFinder):*\n\n"
                    f"🔑 *Keywords rastreadas:*\n{kw_str}\n\n"
                    f"🌐 *Webs de búsqueda:*\n{url_str}"
                )

                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    response_text,
                    parse_mode="Markdown",
                    reply_markup=self.build_main_keyboard()
                )

            # 5. Estado General del Servidor
            elif data == "cmd_view_status":
                status_text = "⚙️ *Estado de DroidServer-AI:*\n\n"
                if os.path.isfile(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            fresh_cfg = json.load(f)
                            ai_mode = fresh_cfg.get("ai", {}).get("mode", "api")
                            model = fresh_cfg.get("ai", {}).get(ai_mode, {}).get("model", "N/A")
                            active = fresh_cfg.get("active_modules", [])
                            status_text += (
                                f"🤖 *Motor IA:* `{ai_mode.upper()}`\n"
                                f"🧠 *Modelo:* `{model}`\n"
                                f"📦 *Módulos activos en cron:* `{', '.join(active) if isinstance(active, list) else active}`\n"
                                f"⏱️ *Frecuencia cron:* Cada 30 minutos\n"
                                f"🚀 *Listener RAM:* ~18 MB (Sin carga de IA)"
                            )
                    except Exception as e:
                        status_text += f"Error leyendo configuración: {e}"
                else:
                    status_text += "Archivo `config.json` no detectado."

                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    call.message.chat.id,
                    status_text,
                    parse_mode="Markdown",
                    reply_markup=self.build_main_keyboard()
                )

        @self.bot.message_handler(func=lambda msg: True, content_types=["text"])
        def handle_incoming_text(message: Message) -> None:
            if not self.is_authorized(message.chat.id):
                logger.warning(f"Unauthorized message from chat_id: {message.chat.id}")
                self.bot.reply_to(message, "⛔ No estás autorizado para enviar notas a este servidor.")
                return

            note_text = message.text.strip()
            if not note_text:
                return

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"note_{timestamp_str}.txt"
            filepath = os.path.join(self.inbox_dir, filename)

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(note_text)
                logger.info(f"Saved new raw note: {filename} ({len(note_text)} chars)")
                self.bot.reply_to(message, "✅ Nota guardada en el inbox. Se procesará en el próximo ciclo.")
            except Exception as e:
                logger.error(f"Failed to persist incoming note: {e}")
                self.bot.reply_to(message, f"❌ Error guardando la nota en el servidor: {e}")

    def start_polling(self) -> None:
        """
        Run resilient long polling loop.
        """
        logger.info("Starting Telegram Listener (Producer engine)...")
        logger.info(f"Authorized Chat ID: {self.authorized_chat_id or 'ANY (Unrestricted)'}")
        logger.info(f"Monitoring inbox directory: {self.inbox_dir}")

        while True:
            try:
                self.bot.infinity_polling(timeout=20, long_polling_timeout=20)
            except Exception as e:
                logger.error(f"Listener polling encountered error: {e}. Reconnecting in 5s...")
                time.sleep(5)


def load_config(config_path: str = CONFIG_PATH) -> Dict[str, Any]:
    """
    Read system configuration from disk.
    """
    if not os.path.isfile(config_path):
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read config file {config_path}: {e}")
        sys.exit(1)


def main() -> None:
    """
    CLI Entrypoint for the long-polling Telegram listener daemon.
    """
    config = load_config()
    telegram_cfg = config.get("telegram", {})
    bot_token = telegram_cfg.get("bot_token", "").strip()
    chat_id = str(telegram_cfg.get("chat_id", "")).strip()

    if not bot_token:
        logger.critical("Missing 'bot_token' in config.json under 'telegram'.")
        sys.exit(1)

    listener = TelegramListener(
        bot_token=bot_token,
        authorized_chat_id=chat_id,
        inbox_dir=INBOX_DIR,
        vault_dir=VAULT_DIR
    )
    listener.start_polling()


if __name__ == "__main__":
    main()
