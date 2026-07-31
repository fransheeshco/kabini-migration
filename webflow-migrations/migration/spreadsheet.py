"""CSV loading and row-level normalization."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

from .exceptions import MigrationError


def clean_text(value: Any) -> Optional[str]:
    """Normalize line endings and blank scalar values."""

    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def load_csv_rows(csv_path: Path) -> list[dict[str, Any]]:
    """Load nonblank UTF-8 CSV rows and attach their source row number."""

    if not csv_path.exists():
        raise MigrationError(f"CSV not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise MigrationError("CSV is empty or has no header row.")
        headers = [str(header).strip() for header in reader.fieldnames]
        if any(not header for header in headers):
            raise MigrationError("CSV contains a blank header.")
        rows: list[dict[str, Any]] = []
        for source_row, raw_row in enumerate(reader, start=2):
            row = {header: clean_text(raw_row.get(header)) for header in headers}
            if any(value is not None for value in row.values()):
                row["_source_row"] = source_row
                rows.append(row)
        return rows


def duplicate_slugs(rows: list[dict[str, Any]]) -> set[str]:
    """Return nonblank slugs that occur more than once."""

    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        slug = str(row.get("slug") or "").strip()
        if slug in seen:
            duplicates.add(slug)
        elif slug:
            seen.add(slug)
    return duplicates
