"""Idempotent TOD Photo Sets collection and reference backfill."""

from __future__ import annotations

import hashlib
import csv
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .exceptions import MigrationError
from .webflow_client import WebflowClient

COLLECTION_NAME = "TOD Photo Sets"
COLLECTION_SLUG = "tod-photo-sets"
REFERENCE_NAME = "Photo Set Reference"
REFERENCE_SLUG = "photo-set-reference"
SEQUENCE_RE = re.compile(r"(?:[-_\s]+)\d+$")
WEBFLOW_ID_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
LEADING_WEBFLOW_IDS_RE = re.compile(r"(?i)^(?:(?:[0-9a-f]{24})[-_]+)+")
GENERATED_PHOTO_PREFIX_RE = re.compile(r"(?i)^photo[-_\s]+\d+[-_\s]+")
GENERIC_PHOTO_NAME_RE = re.compile(r"(?i)^photo[-_\s]*\d+$")


@dataclass(frozen=True)
class PhotoIdentity:
    name: str
    base_slug: str
    source_name: str


def webflow_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.casefold())
    return re.sub(r"-+", "-", text).strip("-")


def humanize_photo_set_name(value: str) -> str:
    """Create a clean display title while preserving punctuation."""

    words = re.sub(r"[-_]+", " ", value).split()
    minor = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to", "with"}
    output = []
    for index, word in enumerate(words):
        lowered = word.casefold()
        if index and lowered.strip("()") in minor:
            output.append(lowered)
        else:
            lowered_word = word.lower()
            first_letter = next(
                (position for position, character in enumerate(lowered_word) if character.isalpha()),
                None,
            )
            output.append(
                lowered_word
                if first_letter is None
                else (
                    lowered_word[:first_letter]
                    + lowered_word[first_letter].upper()
                    + lowered_word[first_letter + 1:]
                )
            )
    return " ".join(output)


def clean_generated_filename(filename: str) -> str:
    """Remove migration IDs/counters without removing an artifact suffix."""

    source = Path(unquote(urlparse(filename).path)).name
    stem = Path(source).stem.strip()
    stem = LEADING_WEBFLOW_IDS_RE.sub("", stem)
    stem = GENERATED_PHOTO_PREFIX_RE.sub("", stem)
    return stem.strip("-_ ")


def derive_photo_set_identity(
    filename: str, *, sequence_suffix_proven: bool = True, gallery_slug: str = ""
) -> PhotoIdentity:
    """Normalize a source filename, stripping a suffix only with evidence."""

    source = Path(unquote(urlparse(filename).path)).name
    cleaned = clean_generated_filename(source)
    normalized_gallery = webflow_slug(gallery_slug)
    if normalized_gallery and webflow_slug(cleaned).startswith(normalized_gallery + "-"):
        cleaned = re.sub(
            rf"(?i)^{re.escape(gallery_slug)}[-_\s]+", "", cleaned, count=1
        )
    if sequence_suffix_proven:
        cleaned = SEQUENCE_RE.sub("", cleaned).strip("-_ ")
    slug = webflow_slug(cleaned)
    if not cleaned or not slug:
        raise MigrationError(f"Cannot derive a Photo Set from filename {source!r}.")
    name = humanize_photo_set_name(cleaned)
    return PhotoIdentity(
        name=name, base_slug=slug, source_name=clean_generated_filename(source)
    )


def usable_photo_name(value: Any) -> bool:
    """Reject generated labels, filenames, and ID-dominated CMS names."""

    name = str(value or "").strip()
    if not name or GENERIC_PHOTO_NAME_RE.fullmatch(name):
        return False
    if WEBFLOW_ID_RE.search(name) or re.search(r"(?i)\.(?:jpe?g|png|gif|webp|avif)$", name):
        return False
    alphanumeric = re.sub(r"[^a-zA-Z0-9]", "", name)
    return bool(alphanumeric) and not re.fullmatch(r"(?i)[0-9a-f]{16,}", alphanumeric)


def deterministic_set_slug(
    base_slug: str, gallery_slug: str, gallery_id: str, *, disambiguate: bool = False
) -> str:
    if not disambiguate:
        return base_slug
    parent = webflow_slug(gallery_slug)
    if not parent:
        parent = hashlib.sha256(gallery_id.encode()).hexdigest()[:10]
    return f"{parent}-{base_slug}"


def _collection_id(collection: dict[str, Any]) -> str:
    return str(collection.get("id") or collection.get("_id") or "").strip()


def _collection_matches(collection: dict[str, Any]) -> bool:
    return collection.get("displayName") == COLLECTION_NAME or collection.get("slug") == COLLECTION_SLUG


def _collection_payload(parent_collection_id: str) -> dict[str, Any]:
    return {
        "displayName": COLLECTION_NAME,
        "singularName": "TOD Photo Set",
        "slug": COLLECTION_SLUG,
        "fields": [
            {"displayName": "Gallery Reference", "slug": "gallery-reference", "type": "Reference", "isRequired": False, "metadata": {"collectionId": parent_collection_id}},
            {"displayName": "Description", "slug": "description", "type": "PlainText", "isRequired": False},
            {"displayName": "Sort Order", "slug": "sort-order", "type": "Number", "isRequired": False},
            {"displayName": "Cover Image", "slug": "cover-image", "type": "Image", "isRequired": False},
        ],
    }


def resolve_photo_sets_collection(
    client: WebflowClient, configured_id: str, parent_collection_id: str, dry_run: bool
) -> tuple[str, bool]:
    collections = client.list_collections()
    if configured_id:
        match = next((c for c in collections if _collection_id(c) == configured_id), None)
        if match is None:
            raise MigrationError("TOD_PHOTO_SETS_COLLECTION_ID does not belong to the configured site.")
        if not _collection_matches(match):
            raise MigrationError("TOD_PHOTO_SETS_COLLECTION_ID is not the TOD Photo Sets collection.")
        return configured_id, False
    match = next((c for c in collections if _collection_matches(c)), None)
    if match:
        return _collection_id(match), False
    if dry_run:
        return "DRY_RUN_TOD_PHOTO_SETS", True
    created = client.create_collection(_collection_payload(parent_collection_id))
    created_id = _collection_id(created)
    if not created_id:
        raise MigrationError("Webflow created TOD Photo Sets without returning an ID.")
    return created_id, True


def _reference_target(field: dict[str, Any]) -> str:
    metadata = field.get("metadata")
    if isinstance(metadata, dict) and metadata.get("collectionId") is not None:
        return str(metadata["collectionId"]).strip()
    logging.warning(
        "Reference field metadata.collectionId is missing: field_id=%s slug=%s "
        "type=%s; checking the validations.collectionId shape returned by Webflow.",
        str(field.get("id") or "").strip(),
        str(field.get("slug") or "").strip(),
        str(field.get("type") or "").strip(),
    )
    validations = field.get("validations")
    if isinstance(validations, dict) and validations.get("collectionId") is not None:
        return str(validations["collectionId"]).strip()
    return ""


def resolve_parent_reference_field(
    schema: dict[str, Any],
    parent_collection_id: str,
    preferred_slug: str = "",
) -> dict[str, Any]:
    """Resolve the one child Reference field targeting the parent collection."""

    parent_id = str(parent_collection_id).strip()
    matches = [
        field
        for field in schema.get("fields", [])
        if field.get("type") == "Reference"
        and _reference_target(field) == parent_id
    ]
    preferred = str(preferred_slug).strip()
    if preferred:
        preferred_matches = [
            field for field in matches
            if str(field.get("slug") or "").strip() == preferred
        ]
        if len(preferred_matches) == 1:
            matches = preferred_matches
    if not matches:
        raise MigrationError(
            "TOD Gallery Photos has no Reference field targeting parent "
            f"Collection {parent_id}."
        )
    if len(matches) != 1:
        slugs = sorted(str(field.get("slug") or "<missing>") for field in matches)
        raise MigrationError(
            "TOD Gallery Photos has multiple Reference fields targeting parent "
            f"Collection {parent_id}: {', '.join(slugs)}. Configure an exact "
            "parent-reference field slug before continuing."
        )
    field = matches[0]
    logging.info(
        "Resolved Gallery Photos parent Reference: "
        "resolved_parent_reference_field_id=%s "
        "resolved_parent_reference_field_slug=%s "
        "resolved_parent_reference_field_display_name=%s "
        "resolved_parent_reference_target_collection_id=%s",
        str(field.get("id") or "").strip(),
        str(field.get("slug") or "").strip(),
        str(field.get("displayName") or "").strip(),
        _reference_target(field),
    )
    return field


def ensure_reference_field(
    client: WebflowClient, photos_collection_id: str, photo_sets_id: str, dry_run: bool
) -> tuple[str, bool]:
    schema = client.get_collection_schema(photos_collection_id)
    candidates = [f for f in schema.get("fields", []) if f.get("slug") == REFERENCE_SLUG]
    if not candidates:
        candidates = [f for f in schema.get("fields", []) if f.get("displayName") == REFERENCE_NAME]
    if candidates:
        field = candidates[0]
        if field.get("type") != "Reference" or _reference_target(field) != photo_sets_id:
            raise MigrationError("Existing Photo Set Reference field has an incompatible type or target collection.")
        return str(field.get("id") or field.get("slug")), False
    if dry_run:
        return REFERENCE_SLUG, True
    created = client.create_collection_field(photos_collection_id, {
        "displayName": REFERENCE_NAME, "slug": REFERENCE_SLUG,
        "type": "Reference", "isRequired": False,
        "metadata": {"collectionId": photo_sets_id},
    })
    return str(created.get("id") or REFERENCE_SLUG), True


def validate_photo_sets_schema(schema: dict[str, Any], parent_collection_id: str) -> None:
    """Fail closed if an existing target collection cannot accept safe payloads."""

    expected = {
        "gallery-reference": "Reference",
        "description": "PlainText",
        "sort-order": "Number",
        "cover-image": "Image",
    }
    fields = {str(field.get("slug")): field for field in schema.get("fields", [])}
    problems: list[str] = []
    for slug, field_type in expected.items():
        field = fields.get(slug)
        if not field:
            problems.append(f"missing {slug}")
        elif field.get("type") != field_type:
            problems.append(f"{slug} is {field.get('type')}, expected {field_type}")
    gallery_field = fields.get("gallery-reference")
    expected_parent_id = str(parent_collection_id).strip()
    if gallery_field:
        actual_parent_id = _reference_target(gallery_field)
        logging.info(
            "Photo Sets schema reference validation: photo_sets_collection_id=%s "
            "configured_parent_collection_id=%s actual_gallery_reference_target_id=%s "
            "field_id=%s field_slug=%s field_type=%s",
            str(schema.get("id") or schema.get("_id") or "").strip(),
            expected_parent_id,
            actual_parent_id or "<missing>",
            str(gallery_field.get("id") or "").strip(),
            str(gallery_field.get("slug") or "").strip(),
            str(gallery_field.get("type") or "").strip(),
        )
        if not actual_parent_id:
            problems.append(
                "gallery-reference has no metadata.collectionId or "
                "validations.collectionId; cannot verify its target"
            )
        elif actual_parent_id != expected_parent_id:
            problems.append(
                "gallery-reference targets a different collection "
                f"(actual={actual_parent_id}, expected={expected_parent_id})"
            )
    if problems:
        raise MigrationError("TOD Photo Sets schema is incompatible: " + "; ".join(problems))


def update_env_value_safely(path: Path, key: str, value: str) -> bool:
    """Replace or append one dotenv value while preserving every other line."""

    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = original.splitlines(keepends=True)
        pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=).*$")
        changed = False
        output: list[str] = []
        for line in lines:
            ending = "\n" if line.endswith("\n") else ""
            match = pattern.match(line.rstrip("\r\n"))
            if match and not changed:
                output.append(f"{match.group(1)}{value}{ending}")
                changed = True
            else:
                output.append(line)
        if not changed:
            if output and not output[-1].endswith("\n"):
                output[-1] += "\n"
            output.append(f"{key}={value}\n")
        path.write_text("".join(output), encoding="utf-8")
        return True
    except OSError:
        return False


def _field_data(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("fieldData")
    return value if isinstance(value, dict) else {}


def _source_filename(fields: dict[str, Any]) -> str:
    for slug in ("original-filename", "source-filename", "image-filename"):
        if fields.get(slug):
            return str(fields[slug])
    image = fields.get("image") or fields.get("photo")
    if isinstance(image, dict):
        for key in ("fileName", "filename", "name", "url"):
            if image.get(key):
                return str(image[key])
    return str(fields.get("name") or "")


def _filename_grouping_source(fields: dict[str, Any]) -> str:
    if any(fields.get(slug) for slug in ("original-filename", "source-filename", "image-filename")):
        return "original filename"
    image = fields.get("image") or fields.get("photo")
    if isinstance(image, dict) and any(
        image.get(key) for key in ("fileName", "filename", "name")
    ):
        return "image metadata"
    return "fallback generated filename"


REVIEW_HEADERS = (
    "parent_gallery_name", "parent_gallery_item_id", "photo_set_name",
    "photo_set_slug", "photo_count", "photo_names", "photo_item_ids",
    "source_values", "grouping_source", "grouping_confidence",
    "is_singleton", "warnings",
)


def write_photo_set_review_csvs(
    rows: list[dict[str, Any]], review_path: Path, singletons_path: Path
) -> None:
    """Write deterministic full and singleton review exports."""

    ordered = sorted(
        rows,
        key=lambda row: (
            str(row["parent_gallery_name"]).casefold(),
            str(row["photo_set_name"]).casefold(),
            str(row["photo_set_slug"]).casefold(),
        ),
    )
    for path, selected in (
        (review_path, ordered),
        (singletons_path, [row for row in ordered if row["is_singleton"] == "true"]),
    ):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW_HEADERS)
            writer.writeheader()
            writer.writerows(selected)


def _reference_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("id", "_id", "itemId", "item_id", "value"):
            if value.get(key) is not None:
                return _reference_value(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return _reference_value(value[0]) if len(value) == 1 else ""
    return str(value or "").strip()


def run_photo_sets(
    client: WebflowClient, *, parent_collection_id: str, photos_collection_id: str,
    configured_photo_sets_id: str = "", dry_run: bool = False,
    limit: int | None = None, env_path: Path = Path(".env"),
    review_path: Path = Path("photo_sets_review.csv"),
    singletons_review_path: Path = Path("photo_sets_singletons_review.csv"),
) -> dict[str, Any]:
    """Validate, group, create/reuse sets, and minimally backfill photos."""

    collections = client.list_collections()
    parent_collection_id = str(parent_collection_id).strip()
    photos_collection_id = str(photos_collection_id).strip()
    ids = {_collection_id(c) for c in collections}
    parent_collection = next(
        (collection for collection in collections if _collection_id(collection) == parent_collection_id),
        None,
    )
    logging.info(
        "Configured parent collection validation: id=%s matched_display_name=%s matched_slug=%s",
        parent_collection_id,
        str((parent_collection or {}).get("displayName") or "<not found>"),
        str((parent_collection or {}).get("slug") or "<not found>"),
    )
    if parent_collection is None or photos_collection_id not in ids:
        raise MigrationError("Configured parent or Gallery Photos collection does not belong to the site.")
    photo_sets_id, collection_created = resolve_photo_sets_collection(
        client, configured_photo_sets_id, parent_collection_id, dry_run
    )
    photo_sets_id = str(photo_sets_id).strip()
    logging.info("Resolved TOD Photo Sets Collection ID: %s", photo_sets_id)
    if not collection_created:
        validate_photo_sets_schema(client.get_collection_schema(photo_sets_id), parent_collection_id)
    photos_schema = client.get_collection_schema(photos_collection_id)
    parent_reference_field = resolve_parent_reference_field(
        photos_schema,
        parent_collection_id,
    )
    parent_reference_slug = str(parent_reference_field.get("slug") or "").strip()
    _, field_created = ensure_reference_field(client, photos_collection_id, photo_sets_id, dry_run)
    photos = client.list_items(photos_collection_id)
    if limit is not None:
        photos = photos[:limit]
    galleries = {_collection_id(i): _field_data(i) for i in client.list_items(parent_collection_id)}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    group_identities: dict[tuple[str, str], PhotoIdentity] = {}
    group_evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    mappings: list[dict[str, Any]] = []
    missing_parent_count = 0
    missing_parent: list[dict[str, Any]] = []
    invalid: list[str] = []
    ambiguous_groups: list[dict[str, Any]] = []
    debug_items_logged = 0
    candidates: list[tuple[dict[str, Any], str, str, str]] = []
    for photo in photos:
        fields = _field_data(photo)
        raw_parent = fields.get(parent_reference_slug)
        parent_id = _reference_value(raw_parent)
        if dry_run and debug_items_logged < 3:
            logging.info(
                "Photo parent Reference debug: photo_item_id=%s photo_name=%r "
                "fieldData_keys=%s resolved_reference_field_slug=%s "
                "raw_parent_reference_value=%r parsed_parent_gallery_item_id=%s",
                str(photo.get("id") or ""),
                str(fields.get("name") or ""),
                sorted(str(key) for key in fields),
                parent_reference_slug,
                raw_parent,
                parent_id or "<empty>",
            )
            debug_items_logged += 1
        if not parent_id or parent_id not in galleries:
            missing_parent_count += 1
            if len(missing_parent) < 10:
                reason = (
                    "reference field absent"
                    if parent_reference_slug not in fields
                    else "reference field empty"
                    if not parent_id
                    else "referenced Gallery item not found"
                )
                missing_parent.append({
                    "photo_item_id": str(photo.get("id") or ""),
                    "photo_name": str(fields.get("name") or ""),
                    "reference_field_slug": parent_reference_slug,
                    "raw_reference_value": raw_parent,
                    "parsed_parent_gallery_item_id": parent_id or None,
                    "reason": reason,
                })
            continue
        source = _source_filename(fields)
        candidates.append((photo, parent_id, source, str(fields.get("name") or "").strip()))

    fallback_bases: dict[tuple[str, str], int] = defaultdict(int)
    for _, parent_id, source, photo_name in candidates:
        if usable_photo_name(photo_name):
            continue
        cleaned = clean_generated_filename(source)
        base = SEQUENCE_RE.sub("", cleaned).strip("-_ ")
        if base and base != cleaned:
            fallback_bases[(parent_id, webflow_slug(base))] += 1

    for photo, parent_id, source, photo_name in candidates:
        try:
            if usable_photo_name(photo_name):
                clean_name = humanize_photo_set_name(photo_name)
                identity = PhotoIdentity(
                    name=clean_name,
                    base_slug=webflow_slug(clean_name),
                    source_name=clean_generated_filename(source),
                )
            else:
                cleaned = clean_generated_filename(source)
                possible_base = SEQUENCE_RE.sub("", cleaned).strip("-_ ")
                has_suffix = possible_base != cleaned
                proven = bool(
                    has_suffix
                    and fallback_bases[(parent_id, webflow_slug(possible_base))] >= 2
                )
                if has_suffix and not proven:
                    ambiguous_groups.append({
                        "photo_item_id": str(photo.get("id") or ""),
                        "photo_name": photo_name,
                        "source": cleaned,
                        "reason": "trailing number may be meaningful; no sibling sequence evidence",
                    })
                    continue
                identity = derive_photo_set_identity(
                    source,
                    sequence_suffix_proven=proven,
                    gallery_slug=str(galleries[parent_id].get("slug") or ""),
                )
            if WEBFLOW_ID_RE.search(identity.name) or WEBFLOW_ID_RE.search(identity.base_slug):
                raise MigrationError("normalized Photo Set identity still contains a Webflow item ID")
        except MigrationError as exc:
            invalid.append(f"photo {photo.get('id')}: {exc}")
            continue
        key = (parent_id, identity.base_slug)
        grouped[key].append(photo)
        group_identities[key] = identity
        group_evidence[key].append({
            "photo_name": photo_name,
            "source_value": identity.source_name,
            "grouping_source": (
                "CMS Name" if usable_photo_name(photo_name)
                else _filename_grouping_source(_field_data(photo))
            ),
            "numeric_suffix_proven": bool(
                not usable_photo_name(photo_name)
                and SEQUENCE_RE.search(clean_generated_filename(source))
                and fallback_bases[(parent_id, webflow_slug(SEQUENCE_RE.sub("", clean_generated_filename(source)).strip("-_ ")))] >= 2
            ),
            "aggressive_normalization": clean_generated_filename(source) != Path(unquote(urlparse(source).path)).stem,
        })

    group_sizes = [len(members) for members in grouped.values()]
    singleton_count = sum(size == 1 for size in group_sizes)
    two_photo_count = sum(size == 2 for size in group_sizes)
    three_plus_count = sum(size >= 3 for size in group_sizes)
    largest_set_size = max(group_sizes, default=0)
    singleton_percentage = (
        singleton_count * 100.0 / len(group_sizes) if group_sizes else 0.0
    )
    malformed = [
        identity for identity in group_identities.values()
        if WEBFLOW_ID_RE.search(identity.name) or WEBFLOW_ID_RE.search(identity.base_slug)
    ]
    if not dry_run:
        if malformed:
            raise MigrationError("Photo Set names or slugs contain Webflow item IDs; refusing to write.")
        if ambiguous_groups:
            raise MigrationError("Ambiguous Photo Set grouping errors remain unresolved; refusing to write.")
        if grouped and singleton_percentage > 80.0:
            raise MigrationError(
                "Photo Set grouping appears ineffective; review normalization before writing."
            )

    parents_by_base_slug: dict[str, set[str]] = defaultdict(set)
    for parent_id, base_slug in grouped:
        parents_by_base_slug[base_slug].add(parent_id)

    review_rows: list[dict[str, Any]] = []
    names_by_parent: dict[str, list[str]] = defaultdict(list)
    for (parent_id, _), identity in group_identities.items():
        names_by_parent[parent_id].append(identity.name)
    for key, members in grouped.items():
        parent_id, base_slug = key
        parent_fields = galleries[parent_id]
        identity = group_identities[key]
        slug = deterministic_set_slug(
            base_slug, str(parent_fields.get("slug") or ""), parent_id,
            disambiguate=len(parents_by_base_slug[base_slug]) > 1,
        )
        evidence = group_evidence[key]
        clean_names = [str(item["photo_name"]).strip() for item in evidence if str(item["photo_name"]).strip()]
        normalized_names = {webflow_slug(name) for name in clean_names}
        sources = {str(item["grouping_source"]) for item in evidence}
        if sources == {"CMS Name"} and len(normalized_names) == 1:
            confidence = "high"
        elif all(item["numeric_suffix_proven"] for item in evidence):
            confidence = "medium"
        else:
            confidence = "low"
        warnings: list[str] = []
        if len(members) == 1:
            warnings.append("singleton set")
        if any(item["aggressive_normalization"] for item in evidence):
            warnings.append("truncated source filename")
        if len(normalized_names) > 1:
            warnings.append("mixed source names")
        if "fallback generated filename" in sources:
            warnings.append("fallback grouping used")
        if len(members) >= 10:
            warnings.append("unusually large set")
        if any(
            other != identity.name
            and (webflow_slug(other).startswith(base_slug) or base_slug.startswith(webflow_slug(other)))
            for other in names_by_parent[parent_id]
        ):
            warnings.append("potential near-duplicate set name within the same Gallery")
        review_rows.append({
            "parent_gallery_name": str(parent_fields.get("name") or parent_fields.get("slug") or ""),
            "parent_gallery_item_id": parent_id,
            "photo_set_name": identity.name,
            "photo_set_slug": slug,
            "photo_count": len(members),
            "photo_names": " | ".join(sorted(set(clean_names), key=str.casefold)),
            "photo_item_ids": " | ".join(sorted(str(member.get("id") or "") for member in members)),
            "source_values": " | ".join(sorted({str(item["source_value"]) for item in evidence}, key=str.casefold)),
            "grouping_source": " | ".join(sorted(sources, key=str.casefold)),
            "grouping_confidence": confidence,
            "is_singleton": "true" if len(members) == 1 else "false",
            "warnings": " | ".join(warnings),
        })

    if dry_run:
        write_photo_set_review_csvs(review_rows, review_path, singletons_review_path)
        logging.info("Photo Sets review CSV: %s", review_path.resolve())
        logging.info("Photo Sets singleton review CSV: %s", singletons_review_path.resolve())

    if photo_sets_id.startswith("DRY_RUN_"):
        existing_sets: list[dict[str, Any]] = []
    else:
        existing_sets = client.list_items(photo_sets_id)
    existing_by_slug = {_field_data(item).get("slug"): item for item in existing_sets}
    set_ids: dict[tuple[str, str], str] = {}
    created = reused = 0
    for key, members in sorted(grouped.items()):
        parent_id, base_slug = key
        parent_fields = galleries[parent_id]
        slug = deterministic_set_slug(
            base_slug,
            str(parent_fields.get("slug") or ""),
            parent_id,
            disambiguate=len(parents_by_base_slug[base_slug]) > 1,
        )
        identity = group_identities[key]
        if WEBFLOW_ID_RE.search(identity.name) or WEBFLOW_ID_RE.search(slug):
            raise MigrationError(f"Unsafe Photo Set identity produced for {slug!r}.")
        existing = existing_by_slug.get(slug)
        if existing:
            existing_parent = _reference_value(_field_data(existing).get("gallery-reference"))
            if existing_parent != parent_id:
                raise MigrationError(f"Photo Set slug collision for {slug!r} with a different Gallery.")
            set_ids[key] = str(existing.get("id") or "")
            reused += 1
            continue
        ordered = sorted(members, key=lambda p: (_field_data(p).get("sort-order") is None, _field_data(p).get("sort-order") or 0, str(p.get("id") or "")))
        first_fields = _field_data(ordered[0])
        data: dict[str, Any] = {"name": identity.name, "slug": slug, "gallery-reference": parent_id}
        if first_fields.get("sort-order") is not None:
            data["sort-order"] = first_fields["sort-order"]
        image = first_fields.get("image") or first_fields.get("photo")
        if image:
            data["cover-image"] = image
        if dry_run:
            set_ids[key] = f"DRY_RUN_SET_{created + 1}"
        else:
            original_collection = client.collection_id
            client.collection_id = photo_sets_id
            try:
                response = client.create_items([{"isArchived": False, "isDraft": True, "fieldData": data}])
            finally:
                client.collection_id = original_collection
            set_ids[key] = str(response.get("id") or response.get("items", [{}])[0].get("id") or "")
            if not set_ids[key]:
                raise MigrationError(f"Webflow did not return an item ID for Photo Set {slug!r}.")
        created += 1
        if len(mappings) < 10:
            mappings.append({
                "gallery": str(parent_fields.get("name") or parent_fields.get("slug") or ""),
                "source": identity.source_name,
                "photo_name": str(_field_data(members[0]).get("name") or ""),
                "photo_set_name": identity.name,
                "photo_set_slug": slug,
                "photos_in_set": len(members),
            })

    linked = already_linked = 0
    failures: list[str] = []
    for key, members in grouped.items():
        set_id = set_ids[key]
        for photo in members:
            current = _reference_value(_field_data(photo).get(REFERENCE_SLUG))
            if current == set_id:
                already_linked += 1
                continue
            if current and current != set_id:
                failures.append(f"Photo {photo.get('id')} already links to a different Photo Set")
                continue
            if not dry_run:
                original_collection = client.collection_id
                client.collection_id = photos_collection_id
                try:
                    client.update_item(str(photo.get("id")), {"fieldData": {REFERENCE_SLUG: set_id}})
                finally:
                    client.collection_id = original_collection
            linked += 1

    if collection_created and not dry_run:
        if not update_env_value_safely(env_path, "TOD_PHOTO_SETS_COLLECTION_ID", photo_sets_id):
            logging.info("Add TOD_PHOTO_SETS_COLLECTION_ID=%s to your .env", photo_sets_id)
    summary = {
        "collection_created": collection_created, "field_created": field_created,
        "photos_inspected": len(photos), "unique_sets": len(grouped),
        "sets_reused": reused, "sets_created": created, "photos_linked": linked,
        "already_linked": already_linked,
        "missing_parent_count": missing_parent_count,
        "missing_parent": missing_parent,
        "invalid_filenames": invalid, "slug_collisions": failures,
        "ambiguous_groups": ambiguous_groups,
        "group_size_summary": {
            "single_photo_sets": singleton_count,
            "two_photo_sets": two_photo_count,
            "three_plus_photo_sets": three_plus_count,
            "largest_set_size": largest_set_size,
            "singleton_percentage": round(singleton_percentage, 2),
        },
        "generated_id_contamination_count": len(malformed),
        "review_csv": str(review_path.resolve()) if dry_run else None,
        "singletons_review_csv": str(singletons_review_path.resolve()) if dry_run else None,
        "low_confidence_count": sum(row["grouping_confidence"] == "low" for row in review_rows),
        "warning_count": sum(bool(row["warnings"]) for row in review_rows),
        "representative_mappings": mappings, "photo_sets_collection_id": photo_sets_id,
    }
    for mapping in mappings:
        logging.info(
            "Photo Set mapping: Gallery: %s | Source: %s | Photo Name: %s | "
            "Photo Set Name: %s | Photo Set Slug: %s | Photos in set: %s",
            mapping["gallery"], mapping["source"], mapping["photo_name"],
            mapping["photo_set_name"], mapping["photo_set_slug"],
            mapping["photos_in_set"],
        )
    logging.info("Photo Sets summary: %s", summary)
    return summary
