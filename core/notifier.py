"""
core/notifier.py - Push Notification Dispatcher via ntfy.

Sends formatted push notifications over HTTP POST to ntfy.sh (or any self-hosted
ntfy instance) requiring zero registration or accounts.
"""

import logging
from typing import Any, Dict, List, Optional
import requests


class Notifier:
    """
    HTTP POST notifier client targeting the open-source ntfy notification protocol.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize the notifier from the notification configuration block.

        :param config: The 'notifications' dictionary from config.json.
        """
        self.config: Dict[str, Any] = config
        self.server: str = self.config.get("server", "https://ntfy.sh").rstrip("/")
        self.topic: str = self.config.get("topic", "").strip()
        self.auth_token: Optional[str] = self.config.get("auth_token")
        self.timeout: int = self.config.get("timeout_seconds", 15)
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)

        if not self.topic:
            raise ValueError("Notification topic cannot be empty. Please configure 'notifications.topic'.")

        self.endpoint: str = f"{self.server}/{self.topic}"
        self.logger.info(f"Notifier initialized for topic: {self.topic} on {self.server}")

    def send(
        self,
        message: str,
        title: Optional[str] = None,
        priority: int = 3,
        tags: Optional[List[str]] = None,
        click_url: Optional[str] = None
    ) -> bool:
        """
        Dispatch a push notification to the configured ntfy topic.

        :param message: Main body of the notification.
        :param title: Optional prominent headline.
        :param priority: Urgency level (1=min, 2=low, 3=default, 4=high, 5=urgent).
        :param tags: Optional list of emoji tags or keyword tags (e.g., ["warning", "tada"]).
        :param click_url: Optional URL to open when the user clicks the notification.
        :return: True if successfully delivered (HTTP 200), False otherwise.
        """
        headers: Dict[str, str] = {
            "Priority": str(priority)
        }

        if title:
            headers["Title"] = title.encode("utf-8").decode("latin-1", errors="replace")
        if tags:
            headers["Tags"] = ",".join(tags)
        if click_url:
            headers["Click"] = click_url
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            self.logger.debug(f"Sending notification to {self.endpoint}: title={title}, priority={priority}")
            response = requests.post(
                self.endpoint,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()
            self.logger.info("Notification successfully dispatched.")
            return True
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to deliver push notification: {e}")
            return False
