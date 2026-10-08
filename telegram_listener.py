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
import glob
import html
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

# Auto-switch to project virtualenv if executed directly outside .venv
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_VENV_PY = os.path.join(_BASE_DIR, ".venv", "bin", "python")
if not os.path.isfile(_VENV_PY):
    _VENV_PY = os.path.join(_BASE_DIR, ".venv", "Scripts", "python.exe")

if os.path.isfile(_VENV_PY) and os.path.abspath(sys.executable) != os.path.abspath(_VENV_PY):
    try:
        import telebot
    except ImportError:
        os.execv(_VENV_PY, [_VENV_PY] + sys.argv)

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    BotCommand,
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
MODELS_DIR = os.path.join(BASE_DIR, "models")
AGENT_SCRIPT = os.path.join(BASE_DIR, "agent.py")
AGENT_LOG = os.path.join(BASE_DIR, "agent_cron.log")
# Must match AGENT_LOCK_PATH / EXIT_ALREADY_RUNNING in agent.py.
AGENT_LOCK_PATH = os.path.join(BASE_DIR, "data", ".agent.lock")
AGENT_EXIT_ALREADY_RUNNING = 75

# Marker used to identify (and replace) our own line inside the user's crontab.
# Must stay in sync with install.sh.
CRON_TAG = "# DroidServer-AI"
DEFAULT_CRON_FREQUENCY = 30
# Only intervals that map to an *exact* cron period are accepted:
# divisors of 60 (minute field) and multiples of 60 whose hour count divides 24.
# e.g. '*/45' would actually fire at :00 and :45 (gaps of 45 and 15 min).
VALID_CRON_MINUTES = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30,
                      60, 120, 180, 240, 360, 480, 720, 1440)
# Quick-pick values offered as buttons in the Status > Frequency submenu.
CRON_QUICK_PICKS = (5, 15, 30, 60, 120, 360)

# Module names accepted by agent.py --module (used by "Run now" buttons).
RUNNABLE_MODULES = {
    "smart_notes": "Smart Notes",
    "task_reminder": "Task Reminder",
    "news_summarizer": "News Summarizer",
    "deal_finder": "Deal Finder",
}

# Registry of user-editable list fields in config.json.
#   key -> (module name, field name, singular label, owning view, kind)
# kind 'url' enforces http(s):// validation.
EDITABLE_LISTS: Dict[str, Tuple[str, str, str, str, str]] = {
    "tasks": ("task_reminder", "tasks", "tarea", "tasks", "text"),
    "news": ("news_summarizer", "urls", "URL de noticias", "news", "url"),
    "dkw": ("deal_finder", "keywords", "keyword", "deals", "text"),
    "durl": ("deal_finder", "urls", "URL de búsqueda", "deals", "url"),
}
MAX_ITEM_LENGTH = 300
MAX_DELETE_BUTTONS = 30
PENDING_INPUT_TTL = 300  # seconds a "type the new item" prompt stays active

# Serializes read-modify-write cycles on config.json across handler threads.
_CONFIG_LOCK = threading.Lock()


# ------------------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------------------
def load_fresh_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Read config.json from disk on every call (returns {} if missing/invalid).
    """
    path = config_path or CONFIG_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Failed to read config file {path}: {e}")
        return {}


def mutate_config(mutator: Callable[[Dict[str, Any]], Any], config_path: Optional[str] = None) -> Any:
    """
    Apply `mutator(cfg)` and persist config.json atomically.

    - A process-wide lock prevents lost updates between concurrent handlers.
    - Writes go to a temp file in the same directory followed by os.replace(),
      so a concurrently running agent.py never reads a half-written JSON file.

    :return: Whatever the mutator returns.
    """
    path = config_path or CONFIG_PATH
    with _CONFIG_LOCK:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        result = mutator(cfg)

        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(cfg, tmp, indent=2, ensure_ascii=False)
                tmp.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return result


def update_config_value(key: str, value: Any, config_path: Optional[str] = None) -> None:
    """
    Atomically update a top-level key in config.json.
    """
    mutate_config(lambda cfg: cfg.__setitem__(key, value), config_path)


def get_editable_list(cfg: Dict[str, Any], list_key: str) -> List[str]:
    """
    Return a copy of an editable list field (empty list if absent/malformed).
    """
    module, field, _, _, _ = EDITABLE_LISTS[list_key]
    value = cfg.get("modules", {}).get(module, {}).get(field, [])
    return [str(v) for v in value] if isinstance(value, list) else []


def item_fingerprint(value: str) -> str:
    """
    Short CRC32 of an item, embedded in delete callbacks so a stale button
    (list changed since it was rendered) can never delete the wrong entry.
    """
    return f"{zlib.crc32(value.encode('utf-8')) & 0xFFFFFFFF:08x}"


def validate_list_item(list_key: str, raw: str) -> Tuple[bool, str]:
    """
    Normalize and validate a user-supplied list item.

    :return: (ok, normalized value or Spanish error message).
    """
    value = " ".join(raw.split())  # collapse newlines / repeated whitespace
    kind = EDITABLE_LISTS[list_key][4]
    if not value:
        return False, "El valor está vacío."
    if len(value) > MAX_ITEM_LENGTH:
        return False, f"Máximo {MAX_ITEM_LENGTH} caracteres."
    if kind == "url":
        if " " in value or not value.lower().startswith(("http://", "https://")):
            return False, "La URL debe empezar por http:// o https:// y no contener espacios."
    return True, value


def add_list_item(list_key: str, value: str, config_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Append an item to an editable list in config.json (duplicates rejected).
    """
    module, field, _, _, _ = EDITABLE_LISTS[list_key]

    def _mutator(cfg: Dict[str, Any]) -> Tuple[bool, str]:
        mod_cfg = cfg.setdefault("modules", {}).setdefault(module, {})
        items = mod_cfg.get(field)
        if not isinstance(items, list):
            items = []
        if any(str(i).strip().lower() == value.lower() for i in items):
            return False, "Ese elemento ya existe."
        items.append(value)
        mod_cfg[field] = items
        return True, value

    return mutate_config(_mutator, config_path)


def remove_list_item(list_key: str, index: int, fingerprint: str, config_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Remove an item by index, only if its fingerprint still matches.
    """
    module, field, _, _, _ = EDITABLE_LISTS[list_key]

    def _mutator(cfg: Dict[str, Any]) -> Tuple[bool, str]:
        items = cfg.get("modules", {}).get(module, {}).get(field)
        if not isinstance(items, list) or not (0 <= index < len(items)):
            return False, "El elemento ya no existe."
        if item_fingerprint(str(items[index])) != fingerprint:
            return False, "La lista ha cambiado. Vuelve a intentarlo."
        return True, str(items.pop(index))

    return mutate_config(_mutator, config_path)


# ------------------------------------------------------------------------------
# Agent execution helpers (manual "Run now")
# ------------------------------------------------------------------------------
def is_agent_running() -> bool:
    """
    Probe the flock held by agent.py. True if a run (cron or manual) is active.
    """
    try:
        import fcntl
    except ImportError:  # Non-POSIX dev machine: cannot detect, assume idle.
        return False
    os.makedirs(os.path.dirname(AGENT_LOCK_PATH), exist_ok=True)
    with open(AGENT_LOCK_PATH, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def get_agent_python() -> str:
    """
    Return project virtualenv python if available, falling back to sys.executable.
    """
    venv_py = os.path.join(BASE_DIR, ".venv", "bin", "python")
    if os.path.isfile(venv_py) and os.access(venv_py, os.X_OK):
        return venv_py
    venv_py_win = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
    if os.path.isfile(venv_py_win):
        return venv_py_win
    return sys.executable


def launch_agent(module: Optional[str] = None) -> subprocess.Popen:
    """
    Spawn agent.py as an independent process (AI is loaded there, never here).

    Output is appended to the same log used by cron. start_new_session detaches
    it from the listener so restarting the listener does not kill a running job.
    """
    cmd = [get_agent_python(), AGENT_SCRIPT]
    if module:
        cmd += ["--module", module]
    log_handle = open(AGENT_LOG, "a", encoding="utf-8")
    try:
        log_handle.write(f"\n--- Manual run from Telegram ({module or 'all active modules'}) "
                         f"at {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        log_handle.flush()
        return subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
    finally:
        log_handle.close()  # Child keeps its own inherited descriptor.


def esc(value: Any) -> str:
    """
    HTML-escape any value for Telegram parse_mode='HTML'.
    """
    return html.escape(str(value), quote=False)


# ------------------------------------------------------------------------------
# Cron helpers (Termux / cronie)
# ------------------------------------------------------------------------------
def minutes_to_cron_schedule(minutes: int) -> str:
    """
    Convert an interval in minutes into the 5-field cron schedule expression.
    """
    if minutes not in VALID_CRON_MINUTES:
        raise ValueError(f"Unsupported interval: {minutes}")
    if minutes < 60:
        return f"*/{minutes} * * * *"
    if minutes == 60:
        return "0 * * * *"
    if minutes == 1440:
        return "0 0 * * *"
    return f"0 */{minutes // 60} * * *"


def format_frequency(minutes: int) -> str:
    """
    Human readable Spanish description of a cron interval.
    """
    if minutes < 60:
        return f"{minutes} minuto{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    return f"{hours} hora{'s' if hours != 1 else ''}"


def _termux_bin(name: str) -> str:
    """
    Resolve a binary from PATH, falling back to Termux's $PREFIX/bin.
    """
    found = shutil.which(name)
    if found:
        return found
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return os.path.join(prefix, "bin", name)


def rewrite_crontab(minutes: int) -> str:
    """
    Replace the DroidServer-AI job in the user's crontab with a new interval.

    - Preserves every other user cron line untouched.
    - Reuses the existing job command (paths, venv, log redirection) when found,
      so only the schedule changes; otherwise rebuilds it from defaults.
    - Ensures the crond daemon is running.

    :return: The cron schedule expression that was installed.
    """
    schedule = minutes_to_cron_schedule(minutes)
    crontab_bin = _termux_bin("crontab")

    current = subprocess.run(
        [crontab_bin, "-l"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10
    )
    # 'crontab -l' exits non-zero when the user has no crontab yet.
    lines: List[str] = current.stdout.splitlines() if current.returncode == 0 else []

    job_command: Optional[str] = None
    kept_lines: List[str] = []
    for line in lines:
        if CRON_TAG in line:
            if job_command is None:
                body = line.split(CRON_TAG, 1)[0].strip()
                parts = body.split(None, 5)  # 5 schedule fields + command
                if len(parts) == 6:
                    job_command = parts[5]
            continue
        kept_lines.append(line)

    if not job_command:
        job_command = (
            f"cd {shlex.quote(BASE_DIR)} && {shlex.quote(sys.executable)} "
            f"{shlex.quote(AGENT_SCRIPT)} >> {shlex.quote(AGENT_LOG)} 2>&1"
        )

    kept_lines.append(f"{schedule} {job_command} {CRON_TAG}")
    new_crontab = "\n".join(kept_lines) + "\n"

    subprocess.run(
        [crontab_bin, "-"],
        input=new_crontab,
        capture_output=True,
        text=True,
        timeout=10,
        check=True
    )

    # Make sure cronie's daemon is alive (it is not started automatically in Termux).
    try:
        alive = subprocess.run(
            [_termux_bin("pgrep"), "crond"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5
        ).returncode == 0
        if not alive:
            subprocess.run(
                [_termux_bin("crond")],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10
            )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning(f"Could not verify/start crond: {e}")

    return schedule


# ------------------------------------------------------------------------------
# Model helpers
# ------------------------------------------------------------------------------
def resolve_model_name(cfg: Dict[str, Any]) -> str:
    """
    Determine the display name of the active model.

    - API mode: ai.api.model.
    - Local mode: basename of ai.local.model_path; if unset, the first *.gguf
      file found in the project's models/ directory.
    """
    ai_cfg = cfg.get("ai", {})
    mode = str(ai_cfg.get("mode", "api")).lower()

    if mode == "local":
        model_path = str(ai_cfg.get("local", {}).get("model_path", "")).strip()
        if model_path:
            name = os.path.basename(model_path)
            return name if os.path.isfile(model_path) else f"{name} (⚠️ no encontrado)"
        ggufs = sorted(glob.glob(os.path.join(MODELS_DIR, "*.gguf")))
        return os.path.basename(ggufs[0]) if ggufs else "N/A"

    return str(ai_cfg.get("api", {}).get("model", "")).strip() or "N/A"


# ------------------------------------------------------------------------------
# High-level operations (Cron & Inbox)
# ------------------------------------------------------------------------------
def apply_cron_frequency(minutes: int) -> Tuple[bool, str]:
    """
    Validate, rewrite crontab, and update config.json atomically.
    Used by both /setcron and the interactive frequency selector buttons.
    """
    if minutes not in VALID_CRON_MINUTES:
        valid_str = ", ".join(str(m) for m in VALID_CRON_MINUTES)
        return False, f"El intervalo {minutes} min no es válido. Opciones: {valid_str}"

    try:
        schedule = rewrite_crontab(minutes)
    except FileNotFoundError:
        return False, "No se encontró el binario 'crontab'. Instálalo con: pkg install cronie"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "").strip() or str(e)
        return False, f"Error actualizando el crontab del sistema: {err}"
    except Exception as e:
        return False, f"Error inesperado actualizando crontab: {e}"

    try:
        update_config_value("cron_frequency", minutes)
    except Exception as e:
        return False, f"Crontab actualizado ({schedule}), pero falló guardar en config.json: {e}"

    return True, f"Frecuencia actualizada. El agente se ejecutará cada {format_frequency(minutes)} ({schedule})."


def clear_inbox(inbox_dir: str = INBOX_DIR) -> Tuple[int, int]:
    """
    Remove all *.txt note files from inbox. Preserves .gitkeep and directories.
    :return: (removed_count, failed_count)
    """
    removed, failed = 0, 0
    for path in glob.glob(os.path.join(inbox_dir, "*.txt")):
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            failed += 1
            logger.error(f"Could not delete inbox file {path}: {e}")
    return removed, failed


class TelegramListener:
    """
    Interactive inbound Telegram listener and note ingest producer.
    Provides in-place inline navigation, submenus for each module, item editing,
    and manual execution triggering without loading heavy AI libraries into memory.
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
        # Tracking interactive input: chat_id -> {"key": list_key, "msg_id": panel_msg_id, "time": timestamp}
        self.pending_inputs: Dict[int, Dict[str, Any]] = {}
        self._register_handlers()

    def is_authorized(self, chat_id: int) -> bool:
        """
        Security gate: verify sender matches authorized chat ID.
        """
        if not self.authorized_chat_id:
            return True
        return str(chat_id) == self.authorized_chat_id

    # --------------------------------------------------------------------------
    # In-place Rendering Engine
    # --------------------------------------------------------------------------
    def _render(
        self,
        chat_id: int,
        message_id: Optional[int],
        text: str,
        markup: Optional[InlineKeyboardMarkup] = None
    ) -> Optional[Message]:
        """
        Render a view by editing the message in-place. If editing fails or if
        message_id is None, sends a fresh message instead.
        """
        if message_id:
            try:
                return self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True
                )
            except ApiTelegramException as e:
                # "message is not modified" happens when user taps refresh or re-selects same state
                if "message is not modified" in str(e).lower():
                    return None
                # Only fall back to fresh message if the target message was deleted or missing
                if "message to edit not found" in str(e).lower() or "message can't be edited" in str(e).lower():
                    logger.warning(f"Message {message_id} cannot be edited; sending fresh.")
                    return self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        disable_web_page_preview=True
                    )
                logger.warning(f"edit_message_text error ({e}); skipping fallback to prevent duplicate messages.")
                return None
            except Exception as e:
                logger.warning(f"Unexpected render edit error: {e}")
                return None

        return self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )

    # --------------------------------------------------------------------------
    # Submenu View Builders
    # --------------------------------------------------------------------------
    def build_main_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("📝 Notas", callback_data="cmd_view_notes"),
            InlineKeyboardButton("📋 Tareas", callback_data="cmd_view_tasks")
        )
        markup.add(
            InlineKeyboardButton("📰 Noticias", callback_data="cmd_view_news"),
            InlineKeyboardButton("🔥 Chollos", callback_data="cmd_view_deals")
        )
        markup.add(
            InlineKeyboardButton("⚡ Estado Servidor", callback_data="cmd_view_status")
        )
        markup.add(
            InlineKeyboardButton("▶️ Ejecutar Todo Ahora", callback_data="cmd_run_all")
        )

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"🤖 <b>DroidServer-AI Control Center</b>\n\n"
            f"{notice_block}"
            f"¡Bienvenido! Tu servidor Android de automatizaciones está activo.\n\n"
            f"• <b>Enviar Notas:</b> Escribe cualquier texto plano y lo guardaré en el inbox.\n"
            f"• <b>Submenús:</b> Toca cualquier botón para gestionar o ejecutar cada módulo.\n\n"
            f"<i>Comandos rápidos:</i> <code>/clear</code> (vaciar inbox), <code>/setcron &lt;min&gt;</code>"
        )
        return text, markup

    def build_notes_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        inbox_files = [
            f for f in os.listdir(self.inbox_dir)
            if os.path.isfile(os.path.join(self.inbox_dir, f)) and not f.startswith(".")
        ]
        md_files = []
        for root, _, files in os.walk(self.vault_dir):
            for file in files:
                if file.endswith(".md"):
                    md_files.append(os.path.relpath(os.path.join(root, file), self.vault_dir))

        vault_summary = ""
        if not md_files:
            vault_summary = "📂 <b>Baúl Markdown:</b> Vacío por el momento."
        else:
            sample = md_files[:10]
            list_str = "\n".join(f"• <code>{esc(f)}</code>" for f in sample)
            more_str = f"\n<i>...y {len(md_files) - 10} notas más.</i>" if len(md_files) > 10 else ""
            vault_summary = f"📂 <b>Baúl Markdown ({len(md_files)} total):</b>\n{list_str}{more_str}"

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"📝 <b>Módulo: Smart Notes</b>\n\n"
            f"{notice_block}"
            f"📥 <b>Pendientes en inbox:</b> {len(inbox_files)} notas\n\n"
            f"{vault_summary}"
        )

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("▶️ Procesar Notas Ahora", callback_data="cmd_run_smart_notes")
        )
        markup.add(
            InlineKeyboardButton("🗑️ Limpiar Inbox", callback_data="cmd_clear_inbox")
        )
        markup.add(
            InlineKeyboardButton("🔄 Actualizar", callback_data="cmd_view_notes"),
            InlineKeyboardButton("🔙 Menú Principal", callback_data="cmd_view_main")
        )
        return text, markup

    def build_tasks_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        cfg = load_fresh_config()
        tasks = get_editable_list(cfg, "tasks")

        if not tasks:
            list_str = "<i>No hay tareas pendientes configuradas.</i>"
        else:
            list_str = "\n".join(f"• {esc(t)}" for t in tasks)

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"📋 <b>Módulo: Task Reminder</b>\n\n"
            f"{notice_block}"
            f"🎯 <b>Tareas Registradas ({len(tasks)}):</b>\n\n"
            f"{list_str}"
        )

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("➕ Añadir Tarea", callback_data="cmd_add_tasks"),
            InlineKeyboardButton("❌ Borrar Tarea", callback_data="cmd_delm_tasks")
        )
        markup.add(
            InlineKeyboardButton("▶️ Ejecutar Recordatorio Ahora", callback_data="cmd_run_task_reminder")
        )
        markup.add(
            InlineKeyboardButton("🔄 Actualizar", callback_data="cmd_view_tasks"),
            InlineKeyboardButton("🔙 Menú Principal", callback_data="cmd_view_main")
        )
        return text, markup

    def build_news_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        cfg = load_fresh_config()
        urls = get_editable_list(cfg, "news")

        if not urls:
            list_str = "<i>No hay feeds o URLs configuradas.</i>"
        else:
            list_str = "\n".join(f"• <code>{esc(u)}</code>" for u in urls)

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"📰 <b>Módulo: News Summarizer</b>\n\n"
            f"{notice_block}"
            f"🌐 <b>Fuentes Monitoreadas ({len(urls)}):</b>\n\n"
            f"{list_str}"
        )

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("➕ Añadir URL", callback_data="cmd_add_news"),
            InlineKeyboardButton("❌ Borrar URL", callback_data="cmd_delm_news")
        )
        markup.add(
            InlineKeyboardButton("▶️ Resumir Noticias Ahora", callback_data="cmd_run_news_summarizer")
        )
        markup.add(
            InlineKeyboardButton("🔄 Actualizar", callback_data="cmd_view_news"),
            InlineKeyboardButton("🔙 Menú Principal", callback_data="cmd_view_main")
        )
        return text, markup

    def build_deals_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        cfg = load_fresh_config()
        keywords = get_editable_list(cfg, "dkw")
        urls = get_editable_list(cfg, "durl")

        kw_str = ", ".join(f"<code>{esc(k)}</code>" for k in keywords) if keywords else "<i>Ninguna</i>"
        url_str = "\n".join(f"• <code>{esc(u)}</code>" for u in urls) if urls else "<i>Ninguna</i>"

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"🔥 <b>Módulo: Deal Finder</b>\n\n"
            f"{notice_block}"
            f"🔑 <b>Palabras Clave ({len(keywords)}):</b>\n{kw_str}\n\n"
            f"🌐 <b>Páginas Rastreadas ({len(urls)}):</b>\n{url_str}"
        )

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("➕ Añadir Keyword", callback_data="cmd_add_dkw"),
            InlineKeyboardButton("❌ Borrar Keyword", callback_data="cmd_delm_dkw")
        )
        markup.add(
            InlineKeyboardButton("➕ Añadir URL", callback_data="cmd_add_durl"),
            InlineKeyboardButton("❌ Borrar URL", callback_data="cmd_delm_durl")
        )
        markup.add(
            InlineKeyboardButton("▶️ Buscar Chollos Ahora", callback_data="cmd_run_deal_finder")
        )
        markup.add(
            InlineKeyboardButton("🔄 Actualizar", callback_data="cmd_view_deals"),
            InlineKeyboardButton("🔙 Menú Principal", callback_data="cmd_view_main")
        )
        return text, markup

    def build_status_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        cfg = load_fresh_config()
        ai_mode = str(cfg.get("ai", {}).get("mode", "api"))
        model = resolve_model_name(cfg)
        active = cfg.get("active_modules", [])
        active_str = ", ".join(active) if isinstance(active, list) else str(active)

        try:
            cron_freq = int(cfg.get("cron_frequency", DEFAULT_CRON_FREQUENCY))
        except (TypeError, ValueError):
            cron_freq = DEFAULT_CRON_FREQUENCY

        running = is_agent_running()
        agent_state = "🟡 <b>En ejecución</b>" if running else "🟢 <b>Disponible</b>"

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"⚙️ <b>Estado de DroidServer-AI</b>\n\n"
            f"{notice_block}"
            f"🤖 <b>Motor IA:</b> <code>{esc(ai_mode.upper())}</code>\n"
            f"🧠 <b>Modelo:</b> <code>{esc(model)}</code>\n"
            f"📦 <b>Módulos activos:</b> <code>{esc(active_str)}</code>\n"
            f"⏱️ <b>Frecuencia cron:</b> Cada {format_frequency(cron_freq)}\n"
            f"⚡ <b>Estado del agente:</b> {agent_state}\n"
            f"🚀 <b>RAM Listener:</b> ~18 MB (sin carga de IA)"
        )

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("⏱️ Cambiar Frecuencia", callback_data="cmd_view_cron")
        )
        markup.add(
            InlineKeyboardButton("▶️ Ejecutar Todo Ahora", callback_data="cmd_run_all")
        )
        markup.add(
            InlineKeyboardButton("🔄 Actualizar", callback_data="cmd_view_status"),
            InlineKeyboardButton("🔙 Menú Principal", callback_data="cmd_view_main")
        )
        return text, markup

    def build_cron_view(self, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        cfg = load_fresh_config()
        try:
            cron_freq = int(cfg.get("cron_frequency", DEFAULT_CRON_FREQUENCY))
        except (TypeError, ValueError):
            cron_freq = DEFAULT_CRON_FREQUENCY

        notice_block = f"{notice}\n\n" if notice else ""
        text = (
            f"⏱️ <b>Frecuencia de Ejecución del Agente</b>\n\n"
            f"{notice_block}"
            f"Frecuencia configurada actual: <b>Cada {format_frequency(cron_freq)}</b>\n\n"
            f"Selecciona un intervalo preestablecido o envía el comando <code>/setcron &lt;minutos&gt;</code>:"
        )

        markup = InlineKeyboardMarkup(row_width=3)
        markup.add(
            InlineKeyboardButton("5 min", callback_data="cmd_cron_5"),
            InlineKeyboardButton("15 min", callback_data="cmd_cron_15"),
            InlineKeyboardButton("30 min", callback_data="cmd_cron_30")
        )
        markup.add(
            InlineKeyboardButton("1 hora", callback_data="cmd_cron_60"),
            InlineKeyboardButton("2 horas", callback_data="cmd_cron_120"),
            InlineKeyboardButton("6 horas", callback_data="cmd_cron_360")
        )
        markup.add(
            InlineKeyboardButton("🔙 Volver a Estado", callback_data="cmd_view_status")
        )
        return text, markup

    def build_delete_picker(self, list_key: str, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        module, field, singular, return_view, kind = EDITABLE_LISTS[list_key]
        cfg = load_fresh_config()
        items = get_editable_list(cfg, list_key)

        notice_block = f"{notice}\n\n" if notice else ""
        markup = InlineKeyboardMarkup(row_width=1)

        if not items:
            text = (
                f"🗑️ <b>Eliminar {esc(singular)}</b>\n\n"
                f"{notice_block}"
                f"<i>No hay elementos registrados para eliminar.</i>"
            )
        else:
            text = (
                f"🗑️ <b>Eliminar {esc(singular)}</b>\n\n"
                f"{notice_block}"
                f"Toca el elemento que deseas eliminar:"
            )
            for i, item in enumerate(items[:MAX_DELETE_BUTTONS]):
                fp = item_fingerprint(item)
                label = item if len(item) <= 35 else item[:32] + "..."
                markup.add(
                    InlineKeyboardButton(
                        f"❌ {label}",
                        callback_data=f"cmd_del_{list_key}_{i}_{fp}"
                    )
                )

        markup.add(
            InlineKeyboardButton("🔙 Volver", callback_data=f"cmd_view_{return_view}")
        )
        return text, markup

    def build_add_prompt(self, list_key: str) -> Tuple[str, InlineKeyboardMarkup]:
        module, field, singular, return_view, kind = EDITABLE_LISTS[list_key]
        hint = "\n<i>(Debe ser una URL válida que empiece por http:// o https://)</i>" if kind == "url" else ""

        text = (
            f"➕ <b>Añadir {esc(singular)}</b>\n\n"
            f"Envía un mensaje de texto con el nuevo valor que deseas registrar.{hint}\n\n"
            f"<i>O pulsa Cancelar para volver sin cambios:</i>"
        )
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton("❌ Cancelar", callback_data="cmd_cancel_input")
        )
        return text, markup

    def build_clear_confirm_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        inbox_files = [
            f for f in os.listdir(self.inbox_dir)
            if os.path.isfile(os.path.join(self.inbox_dir, f)) and not f.startswith(".")
        ]
        text = (
            f"⚠️ <b>¿Vaciar bandeja de entrada (Inbox)?</b>\n\n"
            f"Se eliminarán <b>{len(inbox_files)}</b> nota(s) pendientes de procesar.\n"
            f"Esta acción no se puede deshacer."
        )
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🗑️ Sí, vaciar", callback_data="cmd_clear_confirm"),
            InlineKeyboardButton("🔙 Cancelar", callback_data="cmd_view_notes")
        )
        return text, markup

    def _get_view(self, view_name: str, notice: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        if view_name == "notes":
            return self.build_notes_view(notice)
        elif view_name == "tasks":
            return self.build_tasks_view(notice)
        elif view_name == "news":
            return self.build_news_view(notice)
        elif view_name == "deals":
            return self.build_deals_view(notice)
        elif view_name == "status":
            return self.build_status_view(notice)
        elif view_name == "cron":
            return self.build_cron_view(notice)
        return self.build_main_view(notice)

    # --------------------------------------------------------------------------
    # Agent Execution Engine (Manual Execution + Completion Notifications)
    # --------------------------------------------------------------------------
    def _launch_and_watch(self, chat_id: int, target_mod: Optional[str]) -> Tuple[bool, str]:
        """
        Check concurrency lock and launch agent.py asynchronously.
        Spawns a daemon watcher thread to notify on completion.
        """
        if is_agent_running():
            return False, "El agente ya se encuentra ejecutándose en segundo plano."

        mod_title = RUNNABLE_MODULES.get(target_mod or "", "Todos los módulos activos")

        try:
            proc = launch_agent(target_mod)
        except Exception as e:
            logger.error(f"Failed to launch agent subprocess: {e}")
            return False, f"Error al iniciar el subproceso: {e}"

        def _watcher() -> None:
            code = proc.wait()
            if code == 0:
                logger.info(f"Agent execution completed successfully for: {mod_title}")
            elif code == AGENT_EXIT_ALREADY_RUNNING:
                self.bot.send_message(
                    chat_id,
                    f"ℹ️ <b>{esc(mod_title)}:</b> Se omitió porque ya había otra tarea en curso.",
                    parse_mode="HTML"
                )
            else:
                self.bot.send_message(
                    chat_id,
                    f"⚠️ <b>{esc(mod_title)}:</b> Terminó con código de salida {code}.\n"
                    f"Consulta los detalles en <code>agent_cron.log</code>.",
                    parse_mode="HTML"
                )

        threading.Thread(target=_watcher, daemon=True).start()
        return True, mod_title

    # --------------------------------------------------------------------------
    # TeleBot Event Handlers
    # --------------------------------------------------------------------------
    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=["start", "help"])
        def handle_start(message: Message) -> None:
            if not self.is_authorized(message.chat.id):
                logger.warning(f"Unauthorized access attempt from chat_id: {message.chat.id}")
                self.bot.reply_to(message, "⛔ Acceso denegado: este bot es de uso personal privado.")
                return

            self.pending_inputs.pop(message.chat.id, None)
            text, markup = self.build_main_view()
            self._render(message.chat.id, None, text, markup)

        @self.bot.message_handler(commands=["clear"])
        def handle_clear(message: Message) -> None:
            if not self.is_authorized(message.chat.id):
                self.bot.reply_to(message, "⛔ Acceso denegado.")
                return

            self.pending_inputs.pop(message.chat.id, None)
            removed, failed = clear_inbox(self.inbox_dir)
            logger.info(f"/clear command executed: {removed} removed, {failed} failed")
            reply = f"🗑️ Inbox limpiado ({removed} nota{'s' if removed != 1 else ''} eliminada{'s' if removed != 1 else ''})."
            if failed:
                reply += f"\n⚠️ {failed} archivo(s) no se pudieron borrar."
            self.bot.reply_to(message, reply)

        @self.bot.message_handler(commands=["setcron"])
        def handle_setcron(message: Message) -> None:
            if not self.is_authorized(message.chat.id):
                self.bot.reply_to(message, "⛔ Acceso denegado.")
                return

            self.pending_inputs.pop(message.chat.id, None)
            parts = (message.text or "").split()
            valid_str = ", ".join(str(m) for m in VALID_CRON_MINUTES)
            usage = f"Uso: /setcron <minutos>\nEjemplo: /setcron 15\n\nValores admitidos: {valid_str}"

            if len(parts) != 2 or not parts[1].isdigit():
                self.bot.reply_to(message, f"⚠️ Parámetro inválido.\n\n{usage}")
                return

            minutes = int(parts[1])
            ok, msg = apply_cron_frequency(minutes)
            if ok:
                self.bot.reply_to(message, f"⏱️ {msg}")
            else:
                self.bot.reply_to(message, f"❌ {msg}\n\n{usage}")

        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_callbacks(call: CallbackQuery) -> None:
            chat_id = call.message.chat.id
            message_id = call.message.message_id
            data = call.data

            if not self.is_authorized(chat_id):
                self.bot.answer_callback_query(call.id, "No autorizado.", show_alert=True)
                return

            logger.info(f"Callback query received: {data}")

            # 1. Navigation Views
            if data == "cmd_view_main":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_main_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_notes":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_notes_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_tasks":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_tasks_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_news":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_news_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_deals":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_deals_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_status":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_status_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_view_cron":
                self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_cron_view()
                self._render(chat_id, message_id, text, markup)

            # 2. Add Item Prompt
            elif data.startswith("cmd_add_"):
                list_key = data.replace("cmd_add_", "")
                if list_key in EDITABLE_LISTS:
                    self.pending_inputs[chat_id] = {
                        "key": list_key,
                        "msg_id": message_id,
                        "time": time.time()
                    }
                    self.bot.answer_callback_query(call.id)
                    text, markup = self.build_add_prompt(list_key)
                    self._render(chat_id, message_id, text, markup)

            # 3. Cancel Input
            elif data == "cmd_cancel_input":
                prompt_info = self.pending_inputs.pop(chat_id, None)
                self.bot.answer_callback_query(call.id, "Cancelado")
                return_view = "main"
                if prompt_info and prompt_info.get("key") in EDITABLE_LISTS:
                    return_view = EDITABLE_LISTS[prompt_info["key"]][3]
                text, markup = self._get_view(return_view, notice="ℹ️ Operación cancelada.")
                self._render(chat_id, message_id, text, markup)

            # 4. Open Delete Picker
            elif data.startswith("cmd_delm_"):
                list_key = data.replace("cmd_delm_", "")
                if list_key in EDITABLE_LISTS:
                    self.pending_inputs.pop(chat_id, None)
                    self.bot.answer_callback_query(call.id)
                    text, markup = self.build_delete_picker(list_key)
                    self._render(chat_id, message_id, text, markup)

            # 5. Delete Item Action
            elif data.startswith("cmd_del_"):
                parts = data.split("_", 4)
                if len(parts) == 5:
                    _, _, list_key, idx_str, fp = parts
                    if list_key in EDITABLE_LISTS and idx_str.isdigit():
                        ok, res = remove_list_item(list_key, int(idx_str), fp)
                        if ok:
                            self.bot.answer_callback_query(call.id, f"Eliminado: {res[:20]}")
                            cfg = load_fresh_config()
                            remaining = get_editable_list(cfg, list_key)
                            if remaining:
                                text, markup = self.build_delete_picker(list_key, notice=f"🗑️ Eliminado: <code>{esc(res)}</code>")
                            else:
                                return_view = EDITABLE_LISTS[list_key][3]
                                text, markup = self._get_view(return_view, notice=f"🗑️ Eliminado: <code>{esc(res)}</code>. No quedan más elementos.")
                            self._render(chat_id, message_id, text, markup)
                        else:
                            self.bot.answer_callback_query(call.id, res, show_alert=True)
                            text, markup = self.build_delete_picker(list_key, notice=f"⚠️ {esc(res)}")
                            self._render(chat_id, message_id, text, markup)

            # 6. Change Frequency Buttons
            elif data.startswith("cmd_cron_"):
                min_str = data.replace("cmd_cron_", "")
                if min_str.isdigit():
                    minutes = int(min_str)
                    ok, msg = apply_cron_frequency(minutes)
                    self.bot.answer_callback_query(call.id, "Frecuencia guardada" if ok else "Error")
                    notice = f"✅ {msg}" if ok else f"❌ {msg}"
                    text, markup = self.build_status_view(notice=notice)
                    self._render(chat_id, message_id, text, markup)

            # 7. Inbox Clear Confirmation & Execution
            elif data == "cmd_clear_inbox":
                self.bot.answer_callback_query(call.id)
                text, markup = self.build_clear_confirm_view()
                self._render(chat_id, message_id, text, markup)

            elif data == "cmd_clear_confirm":
                removed, failed = clear_inbox(self.inbox_dir)
                self.bot.answer_callback_query(call.id, "Inbox vaciado")
                notice = f"🗑️ <b>Inbox limpiado:</b> {removed} nota{'s' if removed != 1 else ''} eliminada{'s' if removed != 1 else ''}."
                if failed:
                    notice += f" (⚠️ {failed} no se pudieron borrar)"
                text, markup = self.build_notes_view(notice=notice)
                self._render(chat_id, message_id, text, markup)

            # 8. Run Agent Buttons
            elif data == "cmd_run_all" or data.startswith("cmd_run_"):
                target_mod = None if data == "cmd_run_all" else data.replace("cmd_run_", "")
                started, msg = self._launch_and_watch(chat_id, target_mod)

                if started:
                    self.bot.answer_callback_query(call.id, "🚀 Ejecución iniciada en segundo plano")
                    notice = ""
                else:
                    self.bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
                    notice = f"⚠️ <b>Aviso:</b> {esc(msg)}"

                # Determine which view to refresh
                if target_mod == "smart_notes":
                    text, markup = self.build_notes_view(notice=notice)
                elif target_mod == "task_reminder":
                    text, markup = self.build_tasks_view(notice=notice)
                elif target_mod == "news_summarizer":
                    text, markup = self.build_news_view(notice=notice)
                elif target_mod == "deal_finder":
                    text, markup = self.build_deals_view(notice=notice)
                else:
                    text, markup = self.build_status_view(notice=notice)

                self._render(chat_id, message_id, text, markup)

            else:
                self.bot.answer_callback_query(call.id, "Acción no reconocida")

        @self.bot.message_handler(func=lambda msg: True, content_types=["text"])
        def handle_incoming_text(message: Message) -> None:
            chat_id = message.chat.id
            if not self.is_authorized(chat_id):
                logger.warning(f"Unauthorized text from chat_id: {chat_id}")
                self.bot.reply_to(message, "⛔ No estás autorizado para enviar notas a este servidor.")
                return

            raw_text = (message.text or "").strip()
            if not raw_text:
                return

            # Check if user has an active input prompt
            pending = self.pending_inputs.get(chat_id)
            if pending:
                # Check expiration TTL
                if time.time() - pending.get("time", 0) > PENDING_INPUT_TTL:
                    self.pending_inputs.pop(chat_id, None)
                    pending = None

            if pending:
                list_key = pending["key"]
                panel_msg_id = pending.get("msg_id")

                if raw_text.startswith("/"):
                    # User sent a command while in input mode: cancel input prompt
                    self.pending_inputs.pop(chat_id, None)
                    return_view = EDITABLE_LISTS[list_key][3]
                    text, markup = self._get_view(return_view, notice="ℹ️ Operación cancelada por comando.")
                    self._render(chat_id, panel_msg_id, text, markup)
                    return

                ok, val = validate_list_item(list_key, raw_text)
                if not ok:
                    self.bot.reply_to(message, f"⚠️ {val}\nInténtalo de nuevo o pulsa Cancelar en el panel interactivo.")
                    return

                add_ok, add_res = add_list_item(list_key, val)
                self.pending_inputs.pop(chat_id, None)
                module, field, singular, return_view, _ = EDITABLE_LISTS[list_key]

                if add_ok:
                    notice = f"✅ <b>{esc(singular.capitalize())} añadido:</b> <code>{esc(add_res)}</code>"
                else:
                    notice = f"⚠️ No se pudo añadir: {esc(add_res)}"

                text, markup = self._get_view(return_view, notice=notice)
                self._render(chat_id, panel_msg_id, text, markup)
                return

            # Normal path: user sending a note
            if raw_text.startswith("/"):
                logger.info(f"Ignoring unhandled command (not saved as note): {raw_text.split()[0]}")
                return

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"note_{timestamp_str}.txt"
            filepath = os.path.join(self.inbox_dir, filename)

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(raw_text)
                logger.info(f"Saved new raw note: {filename} ({len(raw_text)} chars)")
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

        try:
            self.bot.set_my_commands([
                BotCommand("start", "Panel de control interactivo"),
                BotCommand("clear", "Borrar notas pendientes del inbox"),
                BotCommand("setcron", "Cambiar frecuencia del agente (minutos)"),
            ])
        except Exception as e:
            logger.warning(f"Could not register bot command menu: {e}")

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
