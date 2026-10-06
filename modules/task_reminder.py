"""
modules/task_reminder.py - Task Prioritization & Daily Reminder Module.

Retrieves pending user tasks from configuration or disk, leverages AI
to categorize and provide actionable productivity strategies, and pushes
a structured briefing to the user's phone via ntfy.
"""

from typing import Any, Dict, List, Union
from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.notifier import Notifier


class TaskReminder(BaseModule):
    """
    Automated task reminder and productivity coaching module.
    """

    def __init__(self, config: Dict[str, Any], ai_handler: AIHandler, notifier: Notifier) -> None:
        super().__init__(config, ai_handler, notifier)
        self.module_cfg: Dict[str, Any] = self.config.get("modules", {}).get("task_reminder", {})

    def execute(self) -> None:
        """
        Extract tasks, request AI coaching/prioritization, and dispatch push notification.
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
            "Keep the response punchy, organized with clean bullet points and emojis, "
            "and ideal for reading on a mobile notification or small screen."
        )

        user_prompt = f"Here is my pending task list for today:\n\n{tasks_formatted}"

        ai_response = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.6,
            max_tokens=600
        )

        self.logger.info("Delivering task briefing via ntfy...")
        success = self.notifier.send(
            message=ai_response,
            title="🎯 Daily Task Masterplan",
            priority=3,
            tags=["calendar", "dart", "white_check_mark"]
        )

        if success:
            self.logger.info("Task reminder successfully delivered.")
        else:
            self.logger.error("Failed to deliver task reminder notification.")
