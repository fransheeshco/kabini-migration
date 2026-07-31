"""Pure payload and gallery-field construction helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def omit_empty_optional_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Remove empty optional values without dropping false or zero."""

    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def valid_image_assets(paths: Sequence[Any]) -> list[Any]:
    """Keep only processed images whose output path exists."""

    return [
        image
        for image in paths
        if getattr(image, "path", None) is not None and image.path.is_file()
    ]
