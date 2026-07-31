"""Reference-field normalization helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .exceptions import MigrationError
from .spreadsheet import clean_text


def normalize_lookup_key(value: Any) -> str:
    """Normalize names and slugs for reference lookup."""

    text = (clean_text(value) or "").casefold()
    return re.sub(r"[\s_-]+", " ", text).strip()


def normalized_aliases(raw_aliases: Any) -> dict[str, str]:
    """Normalize configured reference aliases."""

    if not isinstance(raw_aliases, Mapping):
        return {}
    return {
        normalize_lookup_key(source): str(target).strip()
        for source, target in raw_aliases.items()
        if normalize_lookup_key(source) and clean_text(target)
    }


def resolve_reference_name(
    raw_value: Any,
    lookup: Mapping[str, str],
    aliases: Mapping[str, str],
    *,
    label: str,
    row_slug: str,
) -> Optional[str]:
    """Resolve a source reference name through aliases and an item cache."""

    original = clean_text(raw_value)
    if not original:
        return None
    resolved = aliases.get(normalize_lookup_key(original), original)
    item_id = lookup.get(normalize_lookup_key(resolved))
    if not item_id:
        raise MigrationError(
            f"Could not resolve {label} reference for '{row_slug}': "
            f"CSV value={original!r}, resolved value={resolved!r}."
        )
    return item_id
