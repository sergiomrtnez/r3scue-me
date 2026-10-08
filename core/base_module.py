"""
core/base_module.py - Abstract Base Class for DroidServer-AI Modules.

Every module plugin in DroidServer-AI must inherit from BaseModule and implement
the required abstract methods. This enforces consistent dependency injection,
lifecycle management, and error handling across all system modules.
"""

from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .ai_handler import AIHandler
    from .telegram_outbound import TelegramOutbound


class BaseModule(ABC):
    """
    Abstract Base Class for all DroidServer-AI automation modules.

    Enforces the Template Method and Strategy patterns, ensuring every
    module receives its configuration slice, AI inference handler, and
    outbound Telegram dispatcher upon initialization.
    """

    @abstractmethod
    def __init__(
        self,
        config: Dict[str, Any],
        ai_handler: "AIHandler",
        telegram_outbound: "TelegramOutbound"
    ) -> None:
        """
        Initialize the module instance.

        :param config: Dictionary containing system and module-specific configurations.
        :param ai_handler: Unified AI inference provider (local llama.cpp or cloud API).
        :param telegram_outbound: Outbound Telegram dispatcher for alerts and digests.
        """
        self.config: Dict[str, Any] = config
        self.ai_handler: "AIHandler" = ai_handler
        self.telegram_outbound: "TelegramOutbound" = telegram_outbound
        self.telegram: "TelegramOutbound" = telegram_outbound
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    @property
    def module_name(self) -> str:
        """
        Returns the friendly identifier of the module.
        Defaults to the lowercase class name.
        """
        return self.__class__.__name__.lower()

    @abstractmethod
    def execute(self) -> None:
        """
        Execute the core business logic of the module.

        Must be implemented by concrete subclasses. Handles task retrieval,
        data scraping or note parsing, AI prompt orchestration, and outbound dispatching.
        """
        pass

    def run(self) -> bool:
        """
        Safe execution wrapper providing lifecycle logging and unhandled exception safety.

        :return: True if executed successfully, False otherwise.
        """
        self.logger.info(f"Starting execution of module: {self.module_name}")
        try:
            self.execute()
            self.logger.info(f"Successfully finished execution of module: {self.module_name}")
            return True
        except Exception as e:
            self.logger.exception(f"Unhandled error during execution of module {self.module_name}: {e}")
            try:
                self.telegram_outbound.send_message(
                    text=f"⚠️ *DroidServer-AI Failure*\n\nError in module `{self.module_name}`: {str(e)}"
                )
            except Exception as notify_err:
                self.logger.error(f"Failed to deliver failure notification via Telegram: {notify_err}")
            return False
