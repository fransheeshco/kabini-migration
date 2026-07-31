"""Validation, payload construction, and migration for child photo items."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .description_parser import description_to_rich_text
from .exceptions import MigrationError, ValidationError
from .images import (
    is_hero_image,
    is_special_features_picture,
    normalize_gallery_slug,
    process_single_image,
    read_image_candidate,
    source_path_belongs_to_gallery,
)
from .models import GalleryPhotoRecord

LOGGER = logging.getLogger(__name__)
CHILD_FIELD_ALIASES = {
    "name": ("Photo Name", "Name"),
    "slug": ("Slug",),
    "photo": ("Image", "Photo"),
    "gallery_reference": ("Gallery", "TOD Gallery"),
    "destination_url": ("Destination URL",),
    "sort_order": ("Sort Order",),
    "alt_text": ("Alt Text",),
    "caption": ("Caption",),
    "description": ("Description",),
    "date_or_century": ("Date or Century",),
    "location": ("Location",),
    "material": ("Material",),
    "dimensions": ("Dimensions",),
    "museum_or_collection": ("Museum or Collection",),
    "institution_or_owner": ("Institution or Owner",),
    "accession_number": ("Accession Number",),
    "open_in_new_tab": ("Open in New Tab",),
    "original_image_url": ("Original Image URL",),
    "original_destination_url": ("Original Destination URL",),
    "original_filename": ("Original Filename",),
    "parse_warning": ("Parse Warning",),
}
REQUIRED_CHILD_FIELDS = {
    "name": {"PlainText"},
    "slug": {"PlainText"},
    "photo": {"Image", "ImageRef"},
    "gallery_reference": {"Reference"},
    "sort_order": {"Number"},
}


def load_gallery_photo_rows(path: Path) -> list[GalleryPhotoRecord]:
    """Load generated photo CSV rows into typed records."""

    if not path.is_file():
        raise MigrationError(f"Gallery photo CSV not found: {path}")
    records: list[GalleryPhotoRecord] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        required_groups = {
            "photo name": {"name", "photo_name"},
            "photo slug": {"slug", "photo_slug"},
            "gallery slug": {"gallery_slug"},
            "source path": {"image_path", "source_path"},
            "sort order": {"sort_order"},
        }
        missing = {
            label
            for label, alternatives in required_groups.items()
            if not columns.intersection(alternatives)
        }
        if missing:
            raise MigrationError(
                "Gallery photo CSV is missing columns: " + ", ".join(sorted(missing))
            )
        for source_row, row in enumerate(reader, start=2):
            def value(*names: str) -> str:
                return next(
                    (
                        str(row.get(name) or "").strip()
                        for name in names
                        if str(row.get(name) or "").strip()
                    ),
                    "",
                )

            try:
                sort_order = int(value("sort_order"))
            except ValueError as exc:
                raise MigrationError(
                    f"Invalid sort_order at photo CSV row {source_row}: "
                    f"{row.get('sort_order')!r}"
                ) from exc
            records.append(
                GalleryPhotoRecord(
                    name=value("photo_name", "name"),
                    slug=value("photo_slug", "slug"),
                    gallery_slug=normalize_gallery_slug(
                        row.get("gallery_slug") or ""
                    ),
                    image_filename=value("source_filename", "image_filename"),
                    image_path=value("source_path", "image_path"),
                    image_url=value("image_url"),
                    destination_url=value("destination_url"),
                    alt_text=value("alt_text"),
                    caption=value("caption"),
                    description=value("full_description", "description"),
                    sort_order=sort_order,
                    open_in_new_tab=(row.get("open_in_new_tab") or "").casefold()
                    in {"1", "true", "yes", "on"},
                    original_image_url=value("original_image_url"),
                    original_destination_url=value(
                        "original_destination_url"
                    ),
                    original_filename=value(
                        "original_filename", "source_filename"
                    ),
                    parse_method=value("parse_method"),
                    parse_warning=value("parse_warning"),
                    source_row=source_row,
                    date_or_century=value("date_or_century"),
                    location=value("location"),
                    material=value("material"),
                    dimensions=value("dimensions"),
                    museum_or_collection=value("museum_or_collection"),
                    institution_or_owner=value("institution_or_owner"),
                    accession_number=value("accession_number"),
                )
            )
    return records


def schema_fields_by_display_name(
    schema: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Index a Webflow schema by exact display name."""

    return {
        str(field.get("displayName")): field
        for field in schema.get("fields", [])
        if isinstance(field, Mapping) and field.get("displayName")
    }


def child_fields(schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Detect supported child fields through display-name aliases."""

    by_name = schema_fields_by_display_name(schema)
    detected: dict[str, Mapping[str, Any]] = {}
    for semantic, aliases in CHILD_FIELD_ALIASES.items():
        for display_name in aliases:
            if display_name in by_name:
                detected[semantic] = by_name[display_name]
                break
    return detected


def child_field_slugs(schema: Mapping[str, Any]) -> dict[str, str]:
    """Return semantic field names mapped to actual Webflow API slugs."""

    return {
        semantic: str(field["slug"])
        for semantic, field in child_fields(schema).items()
    }


def validate_child_schema(
    schema: Mapping[str, Any], parent_collection_id: str
) -> list[str]:
    """Validate required fields, types, and the parent reference target."""

    errors: list[str] = []
    fields = child_fields(schema)
    for name, allowed_types in REQUIRED_CHILD_FIELDS.items():
        field = fields.get(name)
        if field is None:
            errors.append(
                "Required child field is missing: "
                + " or ".join(CHILD_FIELD_ALIASES[name])
            )
        elif str(field.get("type")) not in allowed_types:
            errors.append(
                f"Child field '{name}' is {field.get('type')}; expected "
                f"{sorted(allowed_types)}."
            )
    reference = fields.get("gallery_reference", {})
    validations = reference.get("validations") or {}
    if reference and str(validations.get("collectionId", "")) != parent_collection_id:
        errors.append(
            "TOD Gallery reference does not point to the configured parent "
            f"collection {parent_collection_id}."
        )
    return errors


def validate_gallery_photo_rows(
    records: Sequence[GalleryPhotoRecord], parent_slugs: set[str]
) -> tuple[list[str], list[str]]:
    """Validate stable IDs, parent links, files, ordering, and URLs."""

    errors: list[str] = []
    warnings: list[str] = []
    seen_slugs: set[str] = set()
    seen_orders: set[tuple[str, int]] = set()
    seen_photo_sources: set[tuple[str, str, str]] = set()
    accession_records: dict[str, list[str]] = {}
    normalized_parents = {
        normalize_gallery_slug(slug) for slug in parent_slugs
    }
    for record in records:
        context = (
            f"row={record.source_row}, gallery={record.gallery_slug!r}, "
            f"photo={record.name!r}, image={record.image_filename!r}, "
            f"method={record.parse_method!r}"
        )
        if not record.gallery_slug:
            errors.append(f"Missing parent gallery slug ({context}).")
        elif normalize_gallery_slug(record.gallery_slug) not in normalized_parents:
            errors.append(f"Parent gallery does not exist ({context}).")
        if not record.slug:
            errors.append(f"Missing photo slug ({context}).")
        elif record.slug in seen_slugs:
            errors.append(f"Duplicate photo slug {record.slug!r} ({context}).")
        seen_slugs.add(record.slug)
        order_key = (record.gallery_slug, record.sort_order)
        if record.sort_order < 1:
            errors.append(f"Sort order must be positive ({context}).")
        elif order_key in seen_orders:
            errors.append(f"Duplicate sort order {record.sort_order} ({context}).")
        seen_orders.add(order_key)
        source_key = (
            normalize_gallery_slug(record.gallery_slug),
            str(Path(record.image_path)),
            record.destination_url,
        )
        if source_key in seen_photo_sources:
            errors.append(f"Duplicate source photo row ({context}).")
        seen_photo_sources.add(source_key)
        image_path = Path(record.image_path)
        if not record.image_path or not image_path.is_file():
            errors.append(
                f"Local image does not resolve; regenerate the photo CSV ({context})."
            )
        elif not source_path_belongs_to_gallery(
            image_path, record.gallery_slug
        ):
            errors.append(
                "Photo source path belongs to a different gallery "
                f"({context}, source={image_path})."
            )
        elif is_hero_image(image_path):
            errors.append(f"Hero Image cannot be a Gallery Photo ({context}).")
        elif is_special_features_picture(image_path):
            errors.append(
                f"Special Features Picture cannot be a Gallery Photo ({context})."
            )
        if record.destination_url:
            parsed = urlparse(record.destination_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"Invalid destination URL ({context}).")
        else:
            warnings.append(f"Missing destination URL ({context}).")
        if not record.description:
            warnings.append(f"Missing photo description ({context}).")
        if record.parse_warning:
            warnings.append(f"{record.parse_warning} ({context}).")
        for accession in record.accession_number.split("\n\n"):
            accession = accession.strip()
            if accession:
                accession_records.setdefault(accession, []).append(record.slug)
    for accession, slugs in sorted(accession_records.items()):
        if len(slugs) > 1:
            warnings.append(
                f"Duplicate accession number {accession!r}: "
                + ", ".join(slugs)
            )
    return errors, warnings


def build_gallery_photo_payload(
    record: GalleryPhotoRecord,
    schema: Mapping[str, Any],
    parent_item_id: str,
    photo_asset: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a schema-driven Webflow child item payload."""

    detected = child_fields(schema)
    slugs = child_field_slugs(schema)
    values: dict[str, Any] = {
        "name": record.name,
        "slug": record.slug,
        "photo": dict(photo_asset),
        "gallery_reference": parent_item_id,
        "destination_url": record.destination_url or None,
        "sort_order": record.sort_order,
        "alt_text": record.alt_text or None,
        "caption": record.caption or None,
        "date_or_century": record.date_or_century or None,
        "location": record.location or None,
        "material": record.material or None,
        "dimensions": record.dimensions or None,
        "museum_or_collection": record.museum_or_collection or None,
        "institution_or_owner": record.institution_or_owner or None,
        "accession_number": record.accession_number or None,
        "open_in_new_tab": record.open_in_new_tab,
        "original_image_url": record.original_image_url or None,
        "original_destination_url": record.original_destination_url or None,
        "original_filename": record.original_filename or None,
        "parse_warning": record.parse_warning or None,
    }
    description_field = detected.get("description")
    if description_field and record.description:
        values["description"] = (
            description_to_rich_text(record.description, record.name)
            if str(description_field.get("type")) in {"RichText", "Rich Text"}
            else record.description
        )
    field_data = {
        slugs[name]: value
        for name, value in values.items()
        if name in slugs and value is not None and value != ""
    }
    return {"isArchived": False, "isDraft": True, "fieldData": field_data}


def process_photo_image(
    record: GalleryPhotoRecord,
    processed_root: Path,
    *,
    allow_upscaling: bool = False,
) -> Any:
    """Standardize a photo source for child asset upload."""

    path = Path(record.image_path)
    if not source_path_belongs_to_gallery(path, record.gallery_slug):
        raise ValidationError(
            "Gallery photo ownership mismatch: "
            f"current gallery={record.gallery_slug!r}, source={path}."
        )
    if is_hero_image(path) or is_special_features_picture(path):
        raise ValidationError(
            f"Reserved gallery-level image cannot become a Gallery Photo: {path}"
        )
    candidate = read_image_candidate(path, record.gallery_slug)
    if candidate is None:
        return None
    output = (
        processed_root
        / record.gallery_slug
        / f"photo-{record.sort_order:03d}-{record.slug[-24:]}"
    ).with_suffix(".png" if path.suffix.casefold() == ".png" else ".jpg")
    return process_single_image(
        candidate,
        output,
        allow_upscaling=allow_upscaling,
    )


def pending_gallery_photo_records(
    records: Sequence[GalleryPhotoRecord],
    completed: Mapping[str, Mapping[str, Any]],
    existing_slugs: set[str],
    *,
    dry_run: bool,
) -> list[GalleryPhotoRecord]:
    """Retry stale checkpoint entries that are absent from Webflow."""

    pending: list[GalleryPhotoRecord] = []
    for record in records:
        checkpoint_entry = completed.get(record.slug)
        if not checkpoint_entry:
            pending.append(record)
        elif not dry_run and (
            checkpoint_entry.get("status") == "dry_run"
            or record.slug not in existing_slugs
        ):
            LOGGER.warning(
                "Checkpointed Gallery Photo %s is absent from Webflow; "
                "retrying it.",
                record.slug,
            )
            pending.append(record)
    return pending


def migrate_gallery_photos(
    *,
    client: Any,
    records: Sequence[GalleryPhotoRecord],
    schema: Mapping[str, Any],
    parent_ids: Mapping[str, str],
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    processed_root: Path,
    upload_image: Callable[..., dict[str, Any]],
    save_checkpoint: Callable[[Path, Any], None],
    batch_size: int,
    limit: Optional[int],
    dry_run: bool,
    allow_upscaling: bool = False,
    update_existing: bool = False,
) -> dict[str, int]:
    """Create resumable child photo items after parent IDs are resolved."""

    existing = {
        str(item.get("fieldData", {}).get("slug") or ""): str(
            item.get("id") or ""
        )
        for item in ([] if dry_run else client.list_items())
    }
    pending = pending_gallery_photo_records(
        records,
        checkpoint["completed_gallery_photos"],
        set(existing),
        dry_run=dry_run,
    )
    if limit is not None:
        pending = pending[:limit]
    summary = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    for record in pending:
        existing_id = existing.get(record.slug)
        if existing_id and not update_existing:
            checkpoint["skipped_existing_gallery_photos"][record.slug] = {
                "sourceRow": record.source_row,
                "gallerySlug": record.gallery_slug,
            }
            summary["skipped"] += 1
            save_checkpoint(checkpoint_path, checkpoint)
            continue
        normalized_gallery_slug = normalize_gallery_slug(record.gallery_slug)
        parent_id = parent_ids.get(normalized_gallery_slug)
        if not parent_id and dry_run:
            parent_id = f"DRY_RUN_PARENT_{normalized_gallery_slug}"
        if not dry_run and str(parent_id or "").startswith("DRY_RUN"):
            parent_id = None
        if not parent_id:
            checkpoint["failed_gallery_photos"][record.slug] = {
                "error": "Parent Webflow item ID was not resolved.",
                "gallerySlug": record.gallery_slug,
            }
            summary["failed"] += 1
            save_checkpoint(checkpoint_path, checkpoint)
            continue
        try:
            processed = process_photo_image(
                record,
                processed_root,
                allow_upscaling=allow_upscaling,
            )
            if processed is None:
                raise ValidationError("Photo image could not be processed.")
            if processed.gallery_slug != normalized_gallery_slug:
                raise ValidationError(
                    "Processed photo ownership mismatch: "
                    f"current TOD Gallery={normalized_gallery_slug!r}, "
                    f"image gallery={processed.gallery_slug!r}, "
                    f"source={processed.source_path}, "
                    f"processed={processed.path}."
                )
            asset = upload_image(
                client,
                processed,
                alt_text=record.alt_text or record.name,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
                dry_run=dry_run,
                max_size_bytes=4 * 1024 * 1024,
            )
            payload = build_gallery_photo_payload(
                record, schema, parent_id, asset
            )
            if dry_run:
                item_id = f"DRY_RUN_{record.slug}"
            elif existing_id:
                response = client.update_item(existing_id, payload)
                item_id = str(response.get("id") or existing_id)
            else:
                response = client.create_items([payload])
                item_id = str(response.get("id") or "")
            checkpoint["completed_gallery_photos"][record.slug] = {
                "webflowItemId": item_id,
                "gallerySlug": record.gallery_slug,
                "sourceRow": record.source_row,
                "status": (
                    "dry_run"
                    if dry_run
                    else "updated"
                    if existing_id
                    else "created"
                ),
            }
            checkpoint["failed_gallery_photos"].pop(record.slug, None)
            summary["updated" if existing_id else "created"] += 1
        except Exception as exc:
            LOGGER.exception("Failed TOD Gallery Photo %s", record.slug)
            checkpoint["failed_gallery_photos"][record.slug] = {
                "error": str(exc),
                "gallerySlug": record.gallery_slug,
                "sourceRow": record.source_row,
            }
            summary["failed"] += 1
        save_checkpoint(checkpoint_path, checkpoint)
    return summary
