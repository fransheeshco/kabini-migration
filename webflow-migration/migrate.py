#!/usr/bin/env python3
"""Safely migrate Treasury of Discoveries CSV records into Webflow CMS.

Highlights
----------
- Reads tod.csv (plain-text source data)
- Retrieves and saves the exact Webflow collection schema
- Converts plain text into safe Rich Text HTML based on field type
- Resolves Author and Tags reference fields into Webflow CMS item IDs
- Supports author aliases such as user/tkc_admin -> The Kabilin Center
- Matches images from tod-gallery-images/<slug>/ recursively
- Uploads one Hero Image plus all Gallery Images through Webflow Assets
- Creates draft/staged CMS items only
- Skips duplicate Webflow slugs and completed checkpoint slugs
- Writes full per-item request payloads and API responses to logs/
- Supports --slug, --limit, --batch-size, dry-run, and checkpoints

Recommended workflow
--------------------
1. python3 migrate.py inspect
2. python3 migrate.py validate --csv tod.csv
3. python3 migrate.py dry-run --slug in-love-with-mary --limit 1 --batch-size 1
4. python3 migrate.py migrate --slug in-love-with-mary --limit 1 --batch-size 1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv

from email.utils import parsedate_to_datetime

API_BASE = "https://api.webflow.com/v2"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SUPPORTED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"
}
RICH_TEXT_TYPES = {"RichText", "Rich Text"}
REFERENCE_TYPES = {"Reference"}
MULTI_REFERENCE_TYPES = {"MultiReference", "Multi-Reference"}
IMAGE_TYPES = {"Image", "ImageRef"}
MULTI_IMAGE_TYPES = {"MultiImage", "Multi-Image"}

APPROVED_SLUGS = {
    "vestments-and-linens",
    "sacred-furnishings",
    "liturgy-and-the-sacraments-2",
    "holy-men-and-women-of-god",
    "home-devotions",
    "heavenly-host-on-earth",
    "images-of-christ",
    "the-holy-family",
    "in-love-with-mary",
    "painting-the-sacred",
}


class MigrationError(RuntimeError):
    """Raised for recoverable migration/configuration errors."""


def setup_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise MigrationError(f"Required file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"Expected a JSON object in {path}.")
    return value


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def normalize_lookup_key(value: Any) -> str:
    text = clean_text(value) or ""
    text = text.casefold()
    text = re.sub(r"[\s_-]+", " ", text)
    return text.strip()


def normalize_slug(value: Any) -> str:
    text = clean_text(value) or ""
    text = text.casefold().replace("_", "-")
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def split_multi_value(value: Any) -> List[str]:
    text = clean_text(value)
    if not text:
        return []
    values = [piece.strip() for piece in re.split(r"[,;|\n]+", text)]
    return [piece for piece in values if piece]


def plain_text_to_rich_text(value: Any) -> Optional[str]:
    """Convert plain text to conservative Webflow Rich Text HTML.

    Blank lines delimit paragraphs. Single line breaks inside a paragraph become
    <br>. All source text is escaped, so CSV content cannot inject raw HTML.
    """
    text = clean_text(value)
    if not text:
        return None

    paragraphs = re.split(r"\n\s*\n+", text)
    html_paragraphs: List[str] = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.split("\n")]
        escaped_lines = [html.escape(line, quote=False) for line in lines if line]
        if escaped_lines:
            html_paragraphs.append(f"<p>{'<br>'.join(escaped_lines)}</p>")
    return "".join(html_paragraphs) or None


def normalize_date_for_webflow(value: Any) -> Any:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Already ISO-like.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
    except ValueError:
        pass

    # WordPress/RFC 2822 format:
    # Tue, 06 Dec 2022 23:26:27 +0000
    try:
        parsed = parsedate_to_datetime(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
    except (TypeError, ValueError):
        pass

    # Plain calendar date.
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return parsed.replace(tzinfo=timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
    except ValueError:
        pass

    raise MigrationError(
        f"Unsupported publish date format: {text!r}"
    )


def load_csv_rows(csv_path: Path) -> List[Dict[str, Any]]:
    if not csv_path.exists():
        raise MigrationError(f"CSV not found: {csv_path}")

    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise MigrationError("CSV is empty or has no header row.")
        headers = [str(header).strip() for header in reader.fieldnames]
        if any(not header for header in headers):
            raise MigrationError("CSV contains a blank header.")

        rows: List[Dict[str, Any]] = []
        for csv_row_number, raw_row in enumerate(reader, start=2):
            row = {header: clean_text(raw_row.get(header)) for header in headers}
            if not any(value is not None for value in row.values()):
                continue
            row["_source_row"] = csv_row_number
            rows.append(row)
    return rows


class WebflowClient:
    def __init__(
        self,
        token: str,
        site_id: str,
        collection_id: str,
        timeout: int = 60,
        max_retries: int = 5,
    ) -> None:
        self.site_id = site_id
        self.collection_id = collection_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: Tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = f"{method} {url} failed: {exc}"
                if attempt >= self.max_retries:
                    raise MigrationError(last_error) from exc
                delay = min(2 ** attempt, 30)
                logging.warning("Network error. Retrying in %ss: %s", delay, exc)
                time.sleep(delay)
                continue

            if response.status_code in expected:
                return response

            last_error = (
                f"{method} {url} returned {response.status_code}: "
                f"{response.text[:4000]}"
            )
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= self.max_retries
            ):
                raise MigrationError(last_error)

            retry_after = response.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            logging.warning(
                "Temporary Webflow error %s. Retrying in %ss...",
                response.status_code,
                delay,
            )
            time.sleep(delay)

        raise MigrationError(last_error or "Unknown Webflow request failure.")

    def get_collection_schema(self, collection_id: Optional[str] = None) -> Dict[str, Any]:
        target_id = collection_id or self.collection_id
        response = self.request("GET", f"{API_BASE}/collections/{target_id}")
        return response.json()

    def list_items(
        self,
        collection_id: Optional[str] = None,
        page_limit: int = 100,
    ) -> List[Dict[str, Any]]:
        target_id = collection_id or self.collection_id
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            response = self.request(
                "GET",
                f"{API_BASE}/collections/{target_id}/items",
                params={"limit": page_limit, "offset": offset},
            )
            payload = response.json()
            page_items = payload.get("items", [])
            if not isinstance(page_items, list):
                raise MigrationError(
                    f"Unexpected list-items response for collection {target_id}: {payload}"
                )
            items.extend(page_items)
            if len(page_items) < page_limit:
                break
            offset += page_limit
        return items

    def create_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not 1 <= len(items) <= 100:
            raise MigrationError("Each Webflow batch must contain 1-100 items.")

        # Webflow accepts one item object for single creation and an array for a batch.
        payload: Any = items[0] if len(items) == 1 else items
        response = self.request(
            "POST",
            f"{API_BASE}/collections/{self.collection_id}/items",
            expected=(200, 201, 202),
            params={"skipInvalidFiles": "false"},
            json=payload,
        )
        return response.json()

    def upload_asset(self, file_path: Path, alt_text: str = "") -> Dict[str, Any]:
        if not file_path.is_file():
            raise MigrationError(f"Image not found: {file_path}")
        if file_path.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise MigrationError(f"Unsupported image type: {file_path.name}")

        file_bytes = file_path.read_bytes()
        file_hash = hashlib.md5(file_bytes).hexdigest()

        metadata_response = self.request(
            "POST",
            f"{API_BASE}/sites/{self.site_id}/assets",
            expected=(200, 201, 202),
            json={"fileName": file_path.name[:99], "fileHash": file_hash},
        )
        metadata = metadata_response.json()
        upload_url = metadata.get("uploadUrl")
        upload_details = metadata.get("uploadDetails")
        if not upload_url or not isinstance(upload_details, dict):
            raise MigrationError(
                f"Webflow did not return upload details for {file_path.name}: {metadata}"
            )

        content_type = (
            metadata.get("contentType")
            or mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream"
        )
        try:
            upload_response = requests.post(
                upload_url,
                data=upload_details,
                files={"file": (file_path.name, file_bytes, content_type)},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MigrationError(f"Asset upload failed for {file_path.name}: {exc}") from exc

        if upload_response.status_code not in (200, 201, 204):
            raise MigrationError(
                f"Storage upload failed for {file_path.name}: "
                f"{upload_response.status_code} {upload_response.text[:2000]}"
            )

        file_id = metadata.get("id") or metadata.get("fileId")
        hosted_url = metadata.get("hostedUrl") or metadata.get("assetUrl")
        if not file_id or not hosted_url:
            raise MigrationError(
                f"Missing file ID or hosted URL after uploading {file_path.name}: {metadata}"
            )

        return {"fileId": file_id, "url": hosted_url, "alt": alt_text}


def fields_by_slug(schema: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(field["slug"]): dict(field)
        for field in schema.get("fields", [])
        if isinstance(field, Mapping) and field.get("slug")
    }


def field_collection_id(field: Mapping[str, Any]) -> Optional[str]:
    metadata = field.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("collectionId", "collectionID", "collection_id"):
            value = metadata.get(key)
            if value:
                return str(value)

    validations = field.get("validations")
    if isinstance(validations, Mapping):
        for key in ("collectionId", "collectionID", "collection_id"):
            value = validations.get(key)
            if value:
                return str(value)
    return None


def mapped_webflow_slugs(field_map: Mapping[str, Any]) -> List[str]:
    result: List[str] = []
    for target in field_map.get("fields", {}).values():
        if isinstance(target, str) and target:
            result.append(target)
        elif isinstance(target, list):
            result.extend(str(item) for item in target if item)
    return result


def normalize_mapping_targets(target: Any) -> List[str]:
    if target is None:
        return []
    if isinstance(target, str):
        return [target] if target else []
    if isinstance(target, list):
        return [str(value) for value in target if value]
    raise MigrationError(
        "Each field-map target must be null, a Webflow field slug string, or a list of slugs."
    )


def validate_field_map(
    field_map: Dict[str, Any],
    csv_columns: set[str],
    schema: Dict[str, Any],
) -> List[str]:
    errors: List[str] = []
    schema_fields = fields_by_slug(schema)
    mappings = field_map.get("fields")
    if not isinstance(mappings, dict):
        return ["field-map.json must contain a 'fields' object."]

    for csv_column, target in mappings.items():
        if csv_column not in csv_columns:
            errors.append(f"CSV column '{csv_column}' does not exist.")
        try:
            targets = normalize_mapping_targets(target)
        except MigrationError as exc:
            errors.append(f"CSV column '{csv_column}': {exc}")
            continue
        for webflow_slug in targets:
            if webflow_slug not in schema_fields:
                errors.append(
                    f"Webflow field slug '{webflow_slug}' mapped from "
                    f"'{csv_column}' does not exist in the collection schema."
                )

    mapped = set(mapped_webflow_slugs(field_map))
    for required_slug in ("name", "slug"):
        if required_slug not in mapped:
            errors.append(f"Required Webflow field '{required_slug}' is not mapped.")

    image_config = field_map.get("images", {})
    for config_key, allowed_types in (
        ("main_image_field", IMAGE_TYPES),
        ("gallery_field", MULTI_IMAGE_TYPES),
    ):
        slug = image_config.get(config_key)
        if not slug:
            continue
        field = schema_fields.get(slug)
        if not field:
            errors.append(f"Image field '{slug}' configured in '{config_key}' does not exist.")
        elif field.get("type") not in allowed_types:
            errors.append(
                f"Image field '{slug}' is type {field.get('type')}, expected one of "
                f"{sorted(allowed_types)}."
            )

    references = field_map.get("references", {})
    author_field = references.get("author_field", "author")
    tags_field = references.get("tags_field", "tags")
    if author_field in schema_fields and schema_fields[author_field].get("type") not in REFERENCE_TYPES:
        errors.append(f"Author field '{author_field}' is not a Reference field.")
    if tags_field in schema_fields and schema_fields[tags_field].get("type") not in (
        REFERENCE_TYPES | MULTI_REFERENCE_TYPES
    ):
        errors.append(f"Tags field '{tags_field}' is not a Reference/MultiReference field.")

    return errors


def build_reference_lookup(items: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    ambiguous: set[str] = set()

    for item in items:
        item_id = item.get("id")
        field_data = item.get("fieldData", {})
        if not item_id or not isinstance(field_data, Mapping):
            continue
        candidates = [field_data.get("name"), field_data.get("slug")]
        for candidate in candidates:
            key = normalize_lookup_key(candidate)
            if not key:
                continue
            if key in lookup and lookup[key] != item_id:
                ambiguous.add(key)
            else:
                lookup[key] = str(item_id)

    for key in ambiguous:
        lookup.pop(key, None)
    return lookup


def normalized_aliases(raw_aliases: Any) -> Dict[str, str]:
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
    original = clean_text(raw_value)
    if not original:
        return None

    original_key = normalize_lookup_key(original)
    resolved_name = aliases.get(original_key, original)
    resolved_key = normalize_lookup_key(resolved_name)
    item_id = lookup.get(resolved_key)
    if not item_id:
        raise MigrationError(
            f"Could not resolve {label} reference for '{row_slug}': "
            f"CSV value={original!r}, resolved value={resolved_name!r}."
        )

    if resolved_name != original:
        logging.info(
            "%s alias for %s: %r -> %r",
            label,
            row_slug,
            original,
            resolved_name,
        )
    return item_id


def prepare_reference_context(
    client: WebflowClient,
    schema: Dict[str, Any],
    field_map: Dict[str, Any],
) -> Dict[str, Any]:
    schema_fields = fields_by_slug(schema)
    config = field_map.get("references", {})
    author_slug = str(config.get("author_field", "author"))
    tags_slug = str(config.get("tags_field", "tags"))

    context: Dict[str, Any] = {
        "author_field": author_slug,
        "tags_field": tags_slug,
        "author_lookup": {},
        "tags_lookup": {},
        "author_aliases": normalized_aliases(config.get("author_aliases", {})),
        "tag_aliases": normalized_aliases(config.get("tag_aliases", {})),
    }

    for label, slug, lookup_key in (
        ("Author", author_slug, "author_lookup"),
        ("Tags", tags_slug, "tags_lookup"),
    ):
        field = schema_fields.get(slug)
        if not field:
            continue
        collection_id = field_collection_id(field)
        if not collection_id:
            raise MigrationError(
                f"Could not determine referenced collection ID for {label} field '{slug}'. "
                "Inspect collection-schema.json and check the field metadata."
            )
        items = client.list_items(collection_id=collection_id)
        context[lookup_key] = build_reference_lookup(items)
        context[f"{lookup_key}_collection_id"] = collection_id
        logging.info(
            "Loaded %s resolvable %s items from collection %s.",
            len(context[lookup_key]),
            label,
            collection_id,
        )

    return context


def transform_scalar(value: Any, field_type: str) -> Any:
    if value is None:
        return None
    if field_type in RICH_TEXT_TYPES:
        return plain_text_to_rich_text(value)
    if field_type == "DateTime":
        return normalize_date_for_webflow(value)
    if field_type == "Switch":
        lowered = normalize_lookup_key(value)
        if lowered in {"true", "yes", "1", "publish", "published"}:
            return True
        if lowered in {"false", "no", "0", "draft"}:
            return False
        raise MigrationError(f"Invalid Switch value: {value!r}")
    if field_type == "Number":
        try:
            return float(str(value))
        except ValueError as exc:
            raise MigrationError(f"Invalid Number value: {value!r}") from exc
    return value


def find_images_for_slug(images_root: Path, slug: str) -> List[Path]:
    folder = images_root / slug
    if not folder.is_dir():
        return []
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: str(path.relative_to(folder)).casefold(),
    )


def image_cache_key(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    return f"{path.resolve()}::{stat.st_size}::{digest}"


def hero_sort_key(path: Path) -> Tuple[int, str]:
    stem = path.stem.casefold()
    priority = 1
    if any(keyword in stem for keyword in ("hero", "featured", "cover", "banner")):
        priority = 0
    return priority, path.name.casefold()


def upload_images_for_row(
    client: WebflowClient,
    row: Dict[str, Any],
    field_map: Dict[str, Any],
    checkpoint: Dict[str, Any],
    checkpoint_path: Path,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    config = field_map.get("images", {})
    root_value = config.get("root")
    main_field = config.get("main_image_field")
    gallery_field = config.get("gallery_field")
    if not root_value or not (main_field or gallery_field):
        return {}

    slug = str(row.get("slug") or "").strip()
    paths = find_images_for_slug(Path(root_value), slug)
    if not paths:
        message = f"No images found for slug '{slug}' in {Path(root_value) / slug}."
        if config.get("required", False):
            raise MigrationError(message)
        logging.warning(message)
        return {}

    paths = sorted(paths, key=hero_sort_key)
    uploaded: List[Dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        cache_key = image_cache_key(path)
        cached = checkpoint["assets"].get(cache_key)
        if cached:
            logging.info("Reusing uploaded asset: %s", path.name)
            uploaded.append(cached)
            continue

        alt = f"{row.get('title') or slug} - image {index}"
        if dry_run:
            asset = {
                "fileId": f"DRY_RUN_{hashlib.md5(str(path).encode()).hexdigest()[:12]}",
                "url": f"https://example.invalid/{path.name}",
                "alt": alt,
            }
        else:
            logging.info("Uploading image %s/%s: %s", index, len(paths), path)
            asset = client.upload_asset(path, alt_text=alt)
            checkpoint["assets"][cache_key] = asset
            save_json(checkpoint_path, checkpoint)
        uploaded.append(asset)

    result: Dict[str, Any] = {}
    if main_field and uploaded:
        result[str(main_field)] = uploaded[0]
        logging.info("Hero image for %s: %s", slug, paths[0].name)
    if gallery_field:
        gallery_assets = uploaded[1:] if config.get("gallery_excludes_main", False) else uploaded
        result[str(gallery_field)] = gallery_assets
        logging.info("Gallery images for %s: %s", slug, len(gallery_assets))
    return result


def load_checkpoint(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"completed": {}, "failed": {}, "skipped_existing": {}, "assets": {}}
    checkpoint = load_json(path)
    for key in ("completed", "failed", "skipped_existing", "assets"):
        checkpoint.setdefault(key, {})
    return checkpoint


def build_webflow_item(
    row: Dict[str, Any],
    field_map: Dict[str, Any],
    schema: Dict[str, Any],
    reference_context: Dict[str, Any],
    image_fields: Dict[str, Any],
) -> Dict[str, Any]:
    schema_fields = fields_by_slug(schema)
    reference_config = field_map.get("references", {})
    author_csv_column = str(reference_config.get("author_csv_column", "author"))
    tags_csv_column = str(reference_config.get("tags_csv_column", "tags"))
    author_field = str(reference_context.get("author_field", "author"))
    tags_field = str(reference_context.get("tags_field", "tags"))
    row_slug = str(row.get("slug") or "").strip()

    field_data: Dict[str, Any] = {}
    for csv_column, target in field_map.get("fields", {}).items():
        targets = normalize_mapping_targets(target)
        if not targets:
            continue
        value = row.get(csv_column)
        if value is None:
            continue

        for webflow_slug in targets:
            # Reference fields are resolved separately below.
            if webflow_slug in {author_field, tags_field}:
                continue
            field = schema_fields.get(webflow_slug, {})
            transformed = transform_scalar(value, str(field.get("type", "")))
            if transformed is not None:
                field_data[webflow_slug] = transformed

    author_value = row.get(author_csv_column)
    if author_value is not None and author_field in schema_fields:
        author_id = resolve_reference_name(
            author_value,
            reference_context.get("author_lookup", {}),
            reference_context.get("author_aliases", {}),
            label="Author",
            row_slug=row_slug,
        )
        if author_id:
            field_data[author_field] = author_id

    tag_values = split_multi_value(row.get(tags_csv_column))
    if tag_values and tags_field in schema_fields:
        tag_ids: List[str] = []
        for tag in tag_values:
            tag_id = resolve_reference_name(
                tag,
                reference_context.get("tags_lookup", {}),
                reference_context.get("tag_aliases", {}),
                label="Tag",
                row_slug=row_slug,
            )
            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)

        tags_type = schema_fields[tags_field].get("type")
        if tags_type in MULTI_REFERENCE_TYPES:
            field_data[tags_field] = tag_ids
        elif tags_type in REFERENCE_TYPES:
            if len(tag_ids) > 1:
                raise MigrationError(
                    f"Tags field '{tags_field}' is a single Reference but '{row_slug}' "
                    f"contains {len(tag_ids)} tags."
                )
            if tag_ids:
                field_data[tags_field] = tag_ids[0]

    field_data.update(image_fields)

    if not field_data.get("name"):
        raise MigrationError(f"CSV row {row['_source_row']} has no mapped Webflow name.")
    if not field_data.get("slug"):
        raise MigrationError(f"CSV row {row['_source_row']} has no mapped Webflow slug.")

    # Safe invariant: this utility always creates staged draft items.
    return {"isArchived": False, "isDraft": True, "fieldData": field_data}


def extract_created_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if payload.get("id"):
        return [payload]
    return []


def command_inspect(client: WebflowClient, schema_path: Path) -> None:
    schema = client.get_collection_schema()
    save_json(schema_path, schema)
    logging.info(
        "Saved schema for collection '%s' to %s",
        schema.get("displayName", "Unknown"),
        schema_path,
    )
    print("\nWebflow fields:")
    for field in schema.get("fields", []):
        print(
            f"- {field.get('displayName')} | slug={field.get('slug')} | "
            f"type={field.get('type')} | required={field.get('isRequired')} | "
            f"referenceCollection={field_collection_id(field) or '-'}"
        )


def validate_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    slugs = [str(row.get("slug") or "").strip() for row in rows]
    duplicate_slugs = sorted({slug for slug in slugs if slug and slugs.count(slug) > 1})
    if duplicate_slugs:
        errors.append("Duplicate CSV slugs: " + ", ".join(duplicate_slugs))
    blank_rows = [str(row["_source_row"]) for row in rows if not str(row.get("slug") or "").strip()]
    if blank_rows:
        errors.append("Rows with blank slugs: " + ", ".join(blank_rows))
    unexpected = sorted({slug for slug in slugs if slug and slug not in APPROVED_SLUGS})
    if unexpected:
        errors.append("CSV contains unapproved gallery slugs: " + ", ".join(unexpected))
    missing = sorted(APPROVED_SLUGS - set(slugs))
    if missing:
        errors.append("CSV is missing approved gallery slugs: " + ", ".join(missing))
    return errors


def command_validate(
    client: WebflowClient,
    rows: List[Dict[str, Any]],
    schema: Dict[str, Any],
    field_map: Dict[str, Any],
) -> Dict[str, Any]:
    if not rows:
        raise MigrationError("No CSV records were found.")

    columns = set(rows[0].keys()) - {"_source_row"}
    errors = validate_field_map(field_map, columns, schema)
    errors.extend(validate_rows(rows))

    reference_context: Dict[str, Any] = {}
    if not errors:
        try:
            reference_context = prepare_reference_context(client, schema, field_map)
            for row in rows:
                slug = str(row.get("slug") or "").strip()
                author_value = row.get(field_map.get("references", {}).get("author_csv_column", "author"))
                if author_value:
                    resolve_reference_name(
                        author_value,
                        reference_context.get("author_lookup", {}),
                        reference_context.get("author_aliases", {}),
                        label="Author",
                        row_slug=slug,
                    )
                for tag in split_multi_value(
                    row.get(field_map.get("references", {}).get("tags_csv_column", "tags"))
                ):
                    resolve_reference_name(
                        tag,
                        reference_context.get("tags_lookup", {}),
                        reference_context.get("tag_aliases", {}),
                        label="Tag",
                        row_slug=slug,
                    )

                image_config = field_map.get("images", {})
                if image_config.get("required"):
                    root = Path(str(image_config.get("root", "")))
                    if not find_images_for_slug(root, slug):
                        errors.append(f"No required images found for '{slug}' in {root / slug}.")
        except MigrationError as exc:
            errors.append(str(exc))

    if errors:
        print("\nValidation failed:")
        for error in errors:
            print(f"- {error}")
        raise MigrationError(f"Validation found {len(errors)} problem(s).")

    print(
        f"Validation passed: {len(rows)} CSV records, "
        f"{len(schema.get('fields', []))} Webflow fields, "
        f"{len(reference_context.get('author_lookup', {}))} Author lookup entries, "
        f"and {len(reference_context.get('tags_lookup', {}))} Tag lookup entries."
    )
    return reference_context


def command_migrate(
    client: WebflowClient,
    rows: List[Dict[str, Any]],
    schema: Dict[str, Any],
    field_map: Dict[str, Any],
    reference_context: Dict[str, Any],
    checkpoint_path: Path,
    results_path: Path,
    logs_dir: Path,
    *,
    batch_size: int,
    limit: Optional[int],
    selected_slug: Optional[str],
    start_row: Optional[int],
    dry_run: bool,
    skip_existing_webflow_slugs: bool,
) -> None:
    checkpoint = load_checkpoint(checkpoint_path)
    existing_slugs: set[str] = set()
    if skip_existing_webflow_slugs and not dry_run:
        logging.info("Reading existing Webflow items to prevent duplicate slugs...")
        existing_slugs = {
            str(item.get("fieldData", {}).get("slug") or "").strip()
            for item in client.list_items()
        }
        logging.info("Found %s existing Webflow slugs.", len(existing_slugs))

    selected_rows = list(rows)
    if selected_slug:
        normalized_selected = normalize_slug(selected_slug)
        selected_rows = [
            row for row in selected_rows
            if normalize_slug(row.get("slug")) == normalized_selected
        ]
        if not selected_rows:
            raise MigrationError(f"No CSV record found for --slug {selected_slug!r}.")
    if start_row is not None:
        selected_rows = [row for row in selected_rows if row["_source_row"] >= start_row]

    pending: List[Dict[str, Any]] = []
    for row in selected_rows:
        slug = str(row.get("slug") or "").strip()
        if slug in checkpoint["completed"]:
            logging.info("Skipping completed slug: %s", slug)
            continue
        if slug in existing_slugs:
            logging.info("Skipping slug already in Webflow: %s", slug)
            checkpoint["skipped_existing"][slug] = {
                "status": "already_exists",
                "sourceRow": row["_source_row"],
            }
            save_json(checkpoint_path, checkpoint)
            continue
        pending.append(row)

    if limit is not None:
        pending = pending[:limit]
    if not pending:
        logging.info("Nothing to migrate.")
        return

    run_results: List[Dict[str, Any]] = []
    logs_dir.mkdir(parents=True, exist_ok=True)

    for row_batch in chunks(pending, batch_size):
        request_items: List[Dict[str, Any]] = []
        batch_rows: List[Dict[str, Any]] = []

        for row in row_batch:
            slug = str(row.get("slug") or "").strip()
            try:
                image_fields = upload_images_for_row(
                    client,
                    row,
                    field_map,
                    checkpoint,
                    checkpoint_path,
                    dry_run=dry_run,
                )
                item = build_webflow_item(
                    row,
                    field_map,
                    schema,
                    reference_context,
                    image_fields,
                )
                save_json(logs_dir / f"{slug}-payload.json", item)
                logging.info("Final payload saved: %s", logs_dir / f"{slug}-payload.json")
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug("Payload for %s:\n%s", slug, json.dumps(item, indent=2, ensure_ascii=False))
                request_items.append(item)
                batch_rows.append(row)
            except Exception as exc:
                logging.exception("Could not prepare CSV row %s (%s).", row["_source_row"], slug)
                checkpoint["failed"][slug or f"row-{row['_source_row']}"] = {
                    "sourceRow": row["_source_row"],
                    "error": str(exc),
                }
                save_json(checkpoint_path, checkpoint)

        if not request_items:
            continue

        if dry_run:
            response_payload: Any = {
                "items": [
                    {"id": f"DRY_RUN_{row.get('slug')}", **item}
                    for row, item in zip(batch_rows, request_items)
                ]
            }
        else:
            logging.info("Creating Webflow batch of %s draft item(s)...", len(request_items))
            response_payload = client.create_items(request_items)

        created_items = extract_created_items(response_payload)
        for index, row in enumerate(batch_rows):
            slug = str(row.get("slug") or "").strip()
            response_for_item = (
                created_items[index]
                if index < len(created_items)
                else response_payload
            )
            save_json(logs_dir / f"{slug}-response.json", response_for_item)

            created = created_items[index] if index < len(created_items) else {}
            result = {
                "slug": slug,
                "sourceRow": row["_source_row"],
                "webflowItemId": created.get("id"),
                "isDraft": created.get("isDraft", request_items[index].get("isDraft")),
                "status": "dry_run" if dry_run else "created",
            }
            checkpoint["completed"][slug] = result
            checkpoint["failed"].pop(slug, None)
            run_results.append(result)

        save_json(checkpoint_path, checkpoint)
        save_json(results_path, run_results)
        logging.info("Finished batch. Completed in this run: %s", len(run_results))

    logging.info(
        "Migration finished. Results: %s | Checkpoint: %s | Logs: %s",
        results_path,
        checkpoint_path,
        logs_dir,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Treasury of Discoveries CSV content into Webflow CMS."
    )
    parser.add_argument("command", choices=("inspect", "validate", "dry-run", "migrate"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--csv", default="tod.csv")
    parser.add_argument("--field-map", default="field-map.json")
    parser.add_argument("--schema-output", default="collection-schema.json")
    parser.add_argument("--checkpoint", default="migration-checkpoint.json")
    parser.add_argument("--results", default="migration-results.json")
    parser.add_argument("--logs-dir", default="migration-logs")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--slug", default=None, help="Process only one exact gallery slug.")
    parser.add_argument("--start-row", type=int, default=None)
    parser.add_argument(
        "--allow-existing-slugs",
        action="store_true",
        help="Skip the Webflow duplicate-slug query. Not recommended.",
    )
    parser.add_argument(
        "--refresh-schema",
        action="store_true",
        help="Fetch collection-schema.json again before validate/dry-run/migrate.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logs_dir = Path(args.logs_dir)
    setup_logging(args.verbose, logs_dir / "migration.log")
    load_dotenv()

    token = os.getenv("WEBFLOW_API_TOKEN", "").strip()
    site_id = os.getenv("WEBFLOW_SITE_ID", "").strip()
    collection_id = os.getenv("WEBFLOW_COLLECTION_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("WEBFLOW_API_TOKEN", token),
            ("WEBFLOW_SITE_ID", site_id),
            ("WEBFLOW_COLLECTION_ID", collection_id),
        )
        if not value
    ]
    if missing:
        raise MigrationError("Missing environment variables: " + ", ".join(missing))
    if not 1 <= args.batch_size <= 100:
        raise MigrationError("--batch-size must be between 1 and 100.")
    if args.limit is not None and args.limit < 1:
        raise MigrationError("--limit must be at least 1.")

    client = WebflowClient(token, site_id, collection_id)
    schema_path = Path(args.schema_output)

    if args.command == "inspect":
        command_inspect(client, schema_path)
        return 0

    rows = load_csv_rows(Path(args.csv))
    if args.refresh_schema or not schema_path.exists():
        logging.info("Retrieving the latest Webflow collection schema...")
        save_json(schema_path, client.get_collection_schema())
    schema = load_json(schema_path)
    field_map = load_json(Path(args.field_map))

    reference_context = command_validate(client, rows, schema, field_map)
    if args.command == "validate":
        return 0

    command_migrate(
        client,
        rows,
        schema,
        field_map,
        reference_context,
        Path(args.checkpoint),
        Path(args.results),
        logs_dir,
        batch_size=args.batch_size,
        limit=args.limit,
        selected_slug=args.slug,
        start_row=args.start_row,
        dry_run=args.command == "dry-run",
        skip_existing_webflow_slugs=not args.allow_existing_slugs,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.error("Migration interrupted by user.")
        raise SystemExit(130)
    except MigrationError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)