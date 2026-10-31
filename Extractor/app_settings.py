"""Persistent local settings shared by the extractor GUI and CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


FILE_NAME = "extractor_settings.json"


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def settings_path() -> Path:
    return application_directory() / FILE_NAME


def defaults() -> dict[str, object]:
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    return {
        "language": "en",
        "max_threads": min(32, max(1, cpu_count - 4)),
        "always_deep": False,
        "always_log": False,
        "log_path": "_extraction.log",
    }


def load_settings() -> dict[str, object]:
    result = defaults()
    try:
        loaded = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            result.update({key: loaded[key] for key in result if key in loaded})
    except (OSError, ValueError):
        pass
    return result


def save_settings(values: dict[str, object]) -> None:
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def positive_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def configured_workers(default: int) -> int:
    """Return the configured concurrency limit."""
    settings = load_settings()
    env_workers = os.environ.get("UNIVERSAL_SCS_MAX_THREADS")
    workers = positive_int(env_workers, 0) or positive_int(
        settings.get("max_threads"), default) or default
    return max(1, workers)
