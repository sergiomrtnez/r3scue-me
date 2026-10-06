"""
modules/smart_notes.py - Semantic Markdown Note Ingestion & Organization (The Consumer).

Scheduled by cron via agent.py. Scans data/notes_inbox/ for raw .txt notes
dropped by telegram_listener.py, leverages AIHandler to categorize and format
them into clean Markdown, stores them in data/notes_vault/, deletes the raw
intake files, and dispatches a delivery confirmation via TelegramOutbound.
"""

from datetime import datetime
import json
import os
import re
from typing import Any, Dict
from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.telegram_outbound import TelegramOutbound


class SmartNotes(BaseModule):
    """
    Consumer module that processes raw notes from inbox into a structured Markdown vault.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        ai_handler: AIHandler,
        telegram_outbound: TelegramOutbound
    ) -> None:
        super().__init__(config, ai_handler, telegram_outbound)
        self.module_cfg: Dict[str, Any] = self.config.get("modules", {}).get("smart_notes", {})
        self.inbox_dir: str = self.module_cfg.get("inbox_dir", "data/notes_inbox")
        self.vault_dir: str = self.module_cfg.get("vault_dir", "data/notes_vault")

        os.makedirs(self.inbox_dir, exist_ok=True)
        os.makedirs(self.vault_dir, exist_ok=True)

    def _sanitize_filename(self, name: str) -> str:
        """
        Sanitize a string to create a safe filesystem filename.
        """
        sanitized = re.sub(r"[^\w\-_]", "-", name.lower())
        sanitized = re.sub(r"-+", "-", sanitized).strip("-")
        return sanitized[:50] or "note"

    def execute(self) -> None:
        """
        Ingest unorganized .txt notes, classify via AI, save to Markdown vault, and notify.
        """
        # Discover all .txt or .md files in the inbox directory
        inbox_files = [
            os.path.join(self.inbox_dir, f)
            for f in sorted(os.listdir(self.inbox_dir))
            if os.path.isfile(os.path.join(self.inbox_dir, f)) and not f.startswith(".")
        ]

        if not inbox_files:
            self.logger.info("No raw notes found in inbox to process.")
            return

        self.logger.info(f"Found {len(inbox_files)} raw note(s) in inbox. Commencing AI processing...")

        system_prompt = (
            "You are a Second Brain and Knowledge Management AI specialist. "
            "Given an unorganized, quick raw thought or voice note transcript, "
            "your mission is to organize it into a structured Markdown document.\n"
            "Respond strictly with valid JSON matching the following schema:\n"
            "{\n"
            '  "title": "Short descriptive title",\n'
            '  "category": "One overarching category (e.g. Ideas, Projects, Personal, Tech, Finance, Reading)",\n'
            '  "tags": ["tag1", "tag2"],\n'
            '  "markdown_content": "# Title\\n\\n## Summary\\n...\\n\\n## Details / Action Items\\n..."\n'
            "}"
        )

        processed_count = 0
        summary_titles = []

        for file_path in inbox_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw_text = f.read().strip()

                if not raw_text:
                    os.remove(file_path)
                    continue

                self.logger.info(f"Processing inbox note: {os.path.basename(file_path)}")
                organized_info = self._process_single_note(raw_text, system_prompt)
                if organized_info:
                    vault_path = self._save_to_vault(organized_info)
                    summary_titles.append(f"• *{organized_info['title']}* (`{organized_info['category']}`)")
                    processed_count += 1

                # Clean up raw intake file upon successful processing
                os.remove(file_path)

            except Exception as e:
                self.logger.error(f"Failed to process inbox file {file_path}: {e}")

        if processed_count > 0:
            titles_msg = "\n".join(summary_titles)
            notification_text = (
                f"🧠 *He organizado {processed_count} nueva(s) nota(s) en tu baúl:*\n\n"
                f"{titles_msg}"
            )
            self.telegram_outbound.send_message(
                text=notification_text,
                parse_mode="Markdown"
            )
            self.logger.info(f"Ingested and organized {processed_count} notes into vault.")

    def _process_single_note(self, raw_text: str, system_prompt: str) -> Dict[str, Any]:
        """
        Request AI to structure the raw note and parse output JSON.
        """
        user_prompt = f"Raw note to process:\n\n{raw_text}"
        response_text = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=800
        )

        try:
            # Strip markdown json block fences if present
            cleaned = re.sub(r"^```json\s*", "", response_text.strip(), flags=re.MULTILINE)
            cleaned = re.sub(r"```$", "", cleaned.strip(), flags=re.MULTILINE).strip()
            data = json.loads(cleaned)
            return {
                "title": data.get("title", "Untitled Note"),
                "category": data.get("category", "General"),
                "tags": data.get("tags", []),
                "markdown_content": data.get("markdown_content", raw_text)
            }
        except Exception:
            # Fallback if model generates non-JSON text
            first_line = raw_text.split("\n")[0][:30].strip()
            return {
                "title": first_line or "Quick Note",
                "category": "Inbox",
                "tags": ["unclassified"],
                "markdown_content": f"# Quick Note\n\n{response_text}"
            }

    def _save_to_vault(self, note_data: Dict[str, Any]) -> str:
        """
        Save formatted note into the hierarchical vault directory.
        """
        category_dir = os.path.join(self.vault_dir, self._sanitize_filename(note_data["category"]))
        os.makedirs(category_dir, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        slug = self._sanitize_filename(note_data["title"])
        filename = f"{date_str}-{slug}.md"
        target_path = os.path.join(category_dir, filename)

        frontmatter = (
            f"---\n"
            f"title: \"{note_data['title']}\"\n"
            f"date: {datetime.now().isoformat()}\n"
            f"category: \"{note_data['category']}\"\n"
            f"tags: {json.dumps(note_data['tags'])}\n"
            f"---\n\n"
        )

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(frontmatter + note_data["markdown_content"] + "\n")

        self.logger.info(f"Saved note to vault: {target_path}")
        return target_path
