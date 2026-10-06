"""
r3scue-me Core Package.
Provides foundational interfaces for modular plugins, AI handlers, and notifications.
"""

from .base_module import BaseModule
from .ai_handler import AIHandler
from .notifier import Notifier

__all__ = ["BaseModule", "AIHandler", "Notifier"]
