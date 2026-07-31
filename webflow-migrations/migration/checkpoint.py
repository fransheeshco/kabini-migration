"""Atomic checkpoint persistence and resume defaults."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .exceptions import MigrationError

CHECKPOINT_SECTIONS = (
    "completed",
    "failed",
    "skipped_existing",
    "assets",
    "processed_images",
    "completed_gallery_photos",
    "failed_gallery_photos",
    "skipped_existing_gallery_photos",
    "reused_assets",
)


def empty_checkpoint() -> dict[str, Any]:
    """Return a new checkpoint with all supported tracking sections."""

    return {section: {} for section in CHECKPOINT_SECTIONS}


def load_checkpoint(path: Path) -> dict[str, Any]:
    """Read a checkpoint or return a fresh state when it does not exist."""

    if not path.exists():
        return empty_checkpoint()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Invalid checkpoint {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"Expected a JSON object in checkpoint {path}.")
    for section in CHECKPOINT_SECTIONS:
        value.setdefault(section, {})
        if not isinstance(value[section], dict):
            raise MigrationError(
                f"Checkpoint section '{section}' must be a JSON object."
            )
    return value


def save_json_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically so interruption cannot leave a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False, default=str)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
