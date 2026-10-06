"""
core/telegram_outbound.py - Outbound Telegram Notification Dispatcher.

Dispatches notifications and reports directly to the user's Telegram chat via
pure HTTP REST requests (using requests), completely bypassing the need for
heavy polling libraries in batch-scheduled cron jobs.
"""

import logging
from typing import Any, Dict, Optional
import requests


class TelegramOutbound:
    """
    Lightweight Telegram Bot API client for outbound message delivery.
    Operates strictly via direct HTTP POST requests to keep cronjobs lean.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize the Telegram outbound sender.

        :param config: The 'telegram' configuration block from config.json.
        """
        self.config: Dict[str, Any] = config
        self.bot_token: str = self.config.get("bot_token", "").strip()
        self.chat_id: str = str(self.config.get("chat_id", "")).strip()
        self.timeout: int = self.config.get("timeout_seconds", 15)
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)

        if not self.bot_token:
            self.logger.warning("Telegram bot_token is empty in configuration.")
        if not self.chat_id:
            self.logger.warning("Telegram chat_id is empty in configuration.")

        self.api_url: str = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send_message(
        self,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a text message to the configured user chat via Telegram Bot API.

        :param text: Message body (supports auto-splitting if text > 4000 chars).
        :param parse_mode: Formatting mode ('Markdown', 'HTML', or None).
        :param reply_markup: Optional dictionary for inline or custom keyboards.
        :return: True if delivered successfully, False otherwise.
        """
        if not self.bot_token or not self.chat_id:
            self.logger.error("Cannot dispatch message: missing bot_token or chat_id.")
            return False

        if not text:
            self.logger.warning("Attempted to send empty message. Skipping.")
            return False

        # Telegram hard limit is 4096 characters per message
        max_chunk_length = 4000
        chunks = [text[i:i + max_chunk_length] for i in range(0, len(text), max_chunk_length)]

        overall_success = True
        for chunk in chunks:
            payload: Dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": chunk
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup

            try:
                self.logger.debug(f"Sending Telegram outbound chunk ({len(chunk)} chars)...")
                response = requests.post(self.api_url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                result_data = response.json()
                if not result_data.get("ok"):
                    self.logger.error(f"Telegram API responded with error: {result_data.get('description')}")
                    overall_success = False
            except requests.exceptions.RequestException as e:
                self.logger.error(f"HTTP request error sending Telegram message: {e}")
                overall_success = False

        if overall_success:
            self.logger.info("Outbound message successfully sent to Telegram.")
        return overall_success
