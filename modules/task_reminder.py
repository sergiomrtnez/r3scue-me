"""
modules/task_reminder.py - Task Prioritization & Daily Reminder Module.

Retrieves pending user tasks from configuration or disk, leverages AI
to categorize and provide actionable productivity strategies, and pushes
a structured briefing to the user via TelegramOutbound.
"""

from typing import Any, Dict, List, Union
from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.telegram_outbound import TelegramOutbound


class TaskReminder(BaseModule):
    """
    Automated task reminder and productivity coaching module.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        ai_handler: AIHandler,
        telegram_outbound: TelegramOutbound
    ) -> None:
        super().__init__(config, ai_handler, telegram_outbound)
        self.module_cfg: Dict[str, Any] = self.config.get("modules", {}).get("task_reminder", {})

    def execute(self) -> None:
        """
        Extract tasks, request AI coaching/prioritization, and dispatch Telegram notification.
        """
        raw_tasks: Union[List[str], str] = self.module_cfg.get("tasks", [])
        if not raw_tasks:
            self.logger.warning("No tasks found in configuration. Skipping reminder.")
            return

        if isinstance(raw_tasks, list):
            tasks_formatted = "\n".join(f"- {task}" for task in raw_tasks if str(task).strip())
        else:
            tasks_formatted = str(raw_tasks).strip()

        if not tasks_formatted:
            self.logger.warning("Task list is empty after sanitization.")
            return

        self.logger.info("Generating AI productivity breakdown for scheduled tasks...")

        system_prompt = (
            "Eres un asistente personal proactivo, inteligente y motivador. "
            "Tu objetivo es redactar un recordatorio diario personalizado, directo y motivacional "
            "sobre las tareas pendientes específicas del usuario.\n"
            "Instrucciones:\n"
            "1. Sé conciso, enérgico y cercano (en español).\n"
            "2. Prioriza las tareas con claridad y destaca por cuál empezar con determinación.\n"
            "3. Incluye un mensaje motivacional genuino para mantener el foco y evitar la procrastinación.\n"
            "4. Responde ÚNICAMENTE con el mensaje final listo para enviar a Telegram, con emojis y formato limpio."
        )

        user_prompt = f"Estas son mis tareas pendientes para hoy:\n\n{tasks_formatted}"

        texto_ia = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.6,
            max_tokens=600
        )

        texto_ia = (texto_ia or "").strip()
        if not texto_ia:
            self.logger.warning("AI returned an empty response for task reminder.")
            return

        self.logger.info("Delivering AI task briefing via Telegram...")
        self.telegram.send_message(texto_ia)
