"""Pure preflight and runtime validation helpers."""

from __future__ import annotations

from .exceptions import ValidationError


def validate_payload_image_paths(field_data: dict[str, object]) -> None:
    """Reject local image objects pointing at missing output files."""

    for value in field_data.values():
        values = value if isinstance(value, list) else [value]
        for item in values:
            path = getattr(item, "path", None)
            if path is not None and not path.is_file():
                raise ValidationError(f"Payload image does not exist: {path}")
