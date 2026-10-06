#!/usr/bin/env python3
"""
agent.py - Main Orchestrator for r3scue-me.

Loads system configuration, initializes the core AIHandler and Notifier dependencies,
dynamically discovers and validates automation modules inheriting from BaseModule,
and executes them according to the configured schedule or CLI triggers.
"""

import argparse
import importlib
import inspect
import json
import logging
import os
import sys
from typing import Any, Dict, List, Type

from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.notifier import Notifier

# Configure root logging for Termux / Linux stdout and log files
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("r3scue-me")


def load_config(config_path: str) -> Dict[str, Any]:
    """
    Read and validate JSON configuration file.
    """
    if not os.path.isfile(config_path):
        logger.error(f"Configuration file not found at: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format in {config_path}: {e}")
        sys.exit(1)


def discover_module_class(module_name: str) -> Type[BaseModule]:
    """
    Dynamically import a module file and retrieve the class inheriting from BaseModule.
    
    :param module_name: String identifier (e.g., 'task_reminder' or 'news_summarizer').
    :return: Class type extending BaseModule.
    :raises: ImportError or TypeError if no valid BaseModule subclass is found.
    """
    try:
        module_path = f"modules.{module_name}"
        imported_lib = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise ImportError(f"Could not find module file 'modules/{module_name}.py': {e}") from e

    # Find the class that subclasses BaseModule
    candidates: List[Type[BaseModule]] = []
    for _, obj in inspect.getmembers(imported_lib, inspect.isclass):
        if issubclass(obj, BaseModule) and obj is not BaseModule:
            candidates.append(obj)

    if not candidates:
        raise TypeError(f"Module '{module_name}' does not define any class inheriting from BaseModule.")

    return candidates[0]


def main() -> None:
    """
    CLI Entrypoint and orchestrator loop.
    """
    parser = argparse.ArgumentParser(
        description="r3scue-me: Modular Autonomous Automation Server for Android/Linux"
    )
    parser.add_argument(
        "--config", "-c",
        default="config.json",
        help="Path to system configuration JSON file (default: config.json)"
    )
    parser.add_argument(
        "--module", "-m",
        help="Override config and execute a specific module by name (e.g., 'task_reminder')"
    )
    parser.add_argument(
        "--list-modules", "-l",
        action="store_true",
        help="List all available modules in the modules/ directory and exit"
    )

    args = parser.parse_args()

    # Discover available modules on disk
    modules_dir = os.path.join(os.path.dirname(__file__), "modules")
    available_modules = [
        f[:-3] for f in os.listdir(modules_dir)
        if f.endswith(".py") and not f.startswith("__")
    ]

    if args.list_modules:
        print("Available r3scue-me Modules:")
        for m in sorted(available_modules):
            print(f"  - {m}")
        sys.exit(0)

    config = load_config(args.config)

    # Initialize Core Service Singletons
    try:
        ai_handler = AIHandler(config.get("ai", {}))
        notifier = Notifier(config.get("notifications", {}))
    except Exception as e:
        logger.critical(f"Failed to initialize core dependencies: {e}")
        sys.exit(1)

    # Determine which module(s) to execute
    modules_to_run: List[str] = []
    if args.module:
        modules_to_run = [args.module]
    else:
        # Check active_modules list or active_module scalar in config
        active = config.get("active_modules") or config.get("active_module")
        if isinstance(active, list):
            modules_to_run = active
        elif isinstance(active, str):
            modules_to_run = [active]

    if not modules_to_run:
        logger.warning("No active modules specified in configuration or CLI flags.")
        sys.exit(0)

    overall_success = True
    for mod_name in modules_to_run:
        logger.info(f"--- Launching Module: {mod_name} ---")
        try:
            module_class = discover_module_class(mod_name)
            instance = module_class(config, ai_handler, notifier)
            success = instance.run()
            if not success:
                overall_success = False
        except Exception as e:
            logger.exception(f"Fatal error initializing module '{mod_name}': {e}")
            overall_success = False

    sys.exit(0 if overall_success else 1)


if __name__ == "__main__":
    main()
