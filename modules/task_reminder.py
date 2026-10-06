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
            "You are an elite personal executive assistant and productivity coach. "
            "Analyze the user's pending tasks. Your goal is to provide a concise, high-impact "
            "daily action plan: prioritize them using the Eisenhower Matrix (Urgent & Important), "
            "highlight the single #1 'Frog' to eat first, identify any potential blocker, "
            "and give one sharp psychological tip to beat procrastination. "
            "Format the response cleanly with Markdown, emojis, and bullet points for Telegram."
        )

        user_prompt = f"Here is my pending task list for today:\n\n{tasks_formatted}"

        ai_response = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.6,
            max_tokens=600
        )

        message_body = f"🎯 *Daily Task Masterplan*\n\n{ai_response}"
        self.logger.info("Delivering task briefing via Telegram...")
        success = self.telegram_outbound.send_message(
            text=message_body,
            parse_mode="Markdown"
        )

        if success:
            self.logger.info("Task reminder successfully delivered.")
        else:
            self.logger.error("Failed to deliver task reminder notification via Telegram.")
