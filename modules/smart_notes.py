"""
modules/smart_notes.py - Semantic Markdown Note Ingestion & Organization.

Monitors an inbox folder or config entries for unstructured raw notes,
leverages AI to categorize, tag, and reformat them into clean Markdown,
and stores them organized into a structured local knowledge vault.
"""

from datetime import datetime
import json
import os
import re
from typing import Any, Dict, List
from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.notifier import Notifier


class SmartNotes(BaseModule):
    """
    Automated note ingestion, classification, and Markdown vault organizer.
    """

    def __init__(self, config: Dict[str, Any], ai_handler: AIHandler, notifier: Notifier) -> None:
        super().__init__(config, ai_handler, notifier)
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
        Ingest unorganized notes, classify via AI, save to Markdown vault, and notify.
        """
        inbox_files = [
            os.path.join(self.inbox_dir, f)
            for f in os.listdir(self.inbox_dir)
            if os.path.isfile(os.path.join(self.inbox_dir, f)) and not f.startswith(".")
        ]

        # Support direct note entries in config if inbox directory is empty
        config_notes: List[str] = self.module_cfg.get("raw_notes", [])
        if not inbox_files and not config_notes:
            self.logger.info("No raw notes found in inbox or configuration to process.")
            return

        processed_count = 0
        summary_titles = []

        system_prompt = (
            "You are a Second Brain and Knowledge Management AI specialist. "
            "Given an unorganized, quick raw thought or voice note transcript, "
            "your mission is to organize it into a structured Markdown document.\n"
            "Respond strictly with valid JSON with the following schema:\n"
            "{\n"
            '  "title": "Short descriptive title",\n'
            '  "category": "One overarching category (e.g. Ideas, Projects, Personal, Tech, Finance, Reading)",\n'
            '  "tags": ["tag1", "tag2"],\n'
            '  "markdown_content": "# Title\\n\\n## Summary\\n...\\n\\n## Action Items / Details\\n..."\n'
            "}"
        )

        # 1. Process files from inbox
        for file_path in inbox_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw_text = f.read().strip()

                if not raw_text:
                    os.remove(file_path)
                    continue

                self.logger.info(f"Processing note from file: {file_path}")
                organized_info = self._process_single_note(raw_text, system_prompt)
                if organized_info:
                    self._save_to_vault(organized_info)
                    summary_titles.append(f"{organized_info['title']} ({organized_info['category']})")
                    processed_count += 1

                # Remove processed note from inbox to avoid duplicate ingestion
                os.remove(file_path)

            except Exception as e:
                self.logger.error(f"Failed to process inbox file {file_path}: {e}")

        # 2. Process notes passed directly in config
        if config_notes:
            for idx, note_text in enumerate(config_notes):
                if not str(note_text).strip():
                    continue
                self.logger.info(f"Processing config raw note #{idx + 1}")
                organized_info = self._process_single_note(str(note_text).strip(), system_prompt)
                if organized_info:
                    self._save_to_vault(organized_info)
                    summary_titles.append(f"{organized_info['title']} ({organized_info['category']})")
                    processed_count += 1

            # Clear processed config notes
            self.module_cfg["raw_notes"] = []

        if processed_count > 0:
            titles_msg = "\n".join(f"• {t}" for t in summary_titles)
            self.notifier.send(
                message=f"Successfully organized {processed_count} notes into your vault:\n\n{titles_msg}",
                title="🧠 Smart Notes Ingested",
                priority=3,
                tags=["memo", "brain", "books"]
            )
            self.logger.info(f"Ingested and organized {processed_count} notes.")

    def _process_single_note(self, raw_text: str, system_prompt: str) -> Dict[str, Any]:
        """
        Request AI to structure the note and parse output.
        """
        user_prompt = f"Raw note to process:\n\n{raw_text}"
        response_text = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=800
        )

        try:
            # Clean json fences if present
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
            # Fallback if model fails strict JSON formatting
            first_line = raw_text.split("\n")[0][:30]
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
