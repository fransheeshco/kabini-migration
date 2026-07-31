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
- Detects WordPress resize variants from filename dimensions
- Keeps only the highest-resolution copy from each image family
- Uses a G<number> filename such as 099-G3-Icon as the Hero Image
- Keeps Hero Image and Special Features Picture on TOD Galleries
- Migrates ordinary photos only into the related Gallery Photos collection
- Uploads images through Webflow Assets
- Creates draft/staged CMS items only
- Skips duplicate Webflow slugs and completed checkpoint slugs
- Writes full per-item request payloads and API responses to logs/
- Supports --slug, --limit, --batch-size, dry-run, and checkpoints

Important image behavior
------------------------
Given files such as:

    021-1-thumbnail-1-575x1024.jpg
    022-1-thumbnail-1-862x1536.jpg
    023-1-thumbnail-1-1149x2048.jpg

the extraction sequence prefix and trailing dimensions are removed to produce
the same logical key:

    1-thumbnail-1

Only the highest-resolution valid image is used during migration. Local source
files are not deleted by this utility.

Recommended workflow
--------------------
1. python3 migrate.py inspect
2. python3 migrate.py validate --csv tod.csv --refresh-schema
3. python3 migrate.py dry-run --slug images-of-christ --limit 1 --batch-size 1
4. python3 migrate.py migrate --slug images-of-christ --limit 1 --batch-size 1
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv

from .config import (
    DEFAULT_GALLERY_PHOTOS_CSV,
    LEGACY_GALLERY_PHOTOS_CSV,
    PROCESSED_IMAGES_DIRECTORY,
)
from .checkpoint import (
    load_checkpoint as load_checkpoint_state,
    save_json_atomic,
)
from .exceptions import MigrationError
from .gallery_photo_migrator import (
    load_gallery_photo_rows,
    migrate_gallery_photos,
    validate_child_schema,
    validate_gallery_photo_rows,
)
from .logging_config import setup_logging as configure_logging
from .spreadsheet import load_csv_rows as read_csv_rows
from .images import (
    is_special_features_picture as matches_special_features_picture,
    normalize_gallery_slug,
)
from .models import ProcessedImage
from .webflow_client import (
    WebflowClient,
    find_special_features_picture_field,
)
from .photo_sets import run_photo_sets

try:
    from PIL import Image, UnidentifiedImageError
except ImportError as exc:
    raise SystemExit(
        "Pillow is required for image dimension checks. "
        "Install it with: pip install Pillow"
    ) from exc


API_BASE = "https://api.webflow.com/v2"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

SUPPORTED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
}

RICH_TEXT_TYPES = {"RichText", "Rich Text"}
REFERENCE_TYPES = {"Reference"}
MULTI_REFERENCE_TYPES = {"MultiReference", "Multi-Reference"}
IMAGE_TYPES = {"Image", "ImageRef"}
MULTI_IMAGE_TYPES = {"MultiImage", "Multi-Image"}

DEFAULT_MAX_IMAGE_BYTES = 4 * 1024 * 1024
DEFAULT_SPECIAL_FEATURES_FILENAME = "Special-Features-Picture.png"

# Matches names such as:
# 099-G3-Icon.png
# G7-Icon.jpg
# 120_G12_icon.webp
DEFAULT_HERO_PATTERN = r"(?:^|[-_])G\d+(?:[-_]|$)"

# Extraction script prefixes filenames with an ordered number:
# 023-1-thumbnail-1-1149x2048.jpg -> 1-thumbnail-1-1149x2048.jpg
LEADING_SEQUENCE_PREFIX_RE = re.compile(r"^\d{1,7}[-_ ]+")

# WordPress-generated dimensions at the end of a filename stem:
# photo-300x152, photo-1149x2048
DIMENSION_SUFFIX_RE = re.compile(
    r"-(?P<width>\d{2,6})x(?P<height>\d{2,6})$",
    flags=re.IGNORECASE,
)

WORDPRESS_GENERATED_SUFFIX_RE = re.compile(
    r"-(scaled|rotated|edited)$",
    flags=re.IGNORECASE,
)

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


@dataclass(frozen=True)
class LocalImage:
    """Metadata used to select the best image variant."""

    path: Path
    logical_key: str
    width: int
    height: int
    size_bytes: int
    filename_width: Optional[int]
    filename_height: Optional[int]
    has_dimension_suffix: bool
    is_hero: bool

    @property
    def pixel_area(self) -> int:
        return self.width * self.height


def setup_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    configure_logging(verbose, log_file)


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def save_json(path: Path, data: Any) -> None:
    save_json_atomic(path, data)


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
    """Convert plain text to conservative Webflow Rich Text HTML."""

    text = clean_text(value)
    if not text:
        return None

    paragraphs = re.split(r"\n\s*\n+", text)
    html_paragraphs: List[str] = []

    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.split("\n")]
        escaped_lines = [
            html.escape(line, quote=False)
            for line in lines
            if line
        ]

        if escaped_lines:
            html_paragraphs.append(
                f"<p>{'<br>'.join(escaped_lines)}</p>"
            )

    return "".join(html_paragraphs) or None


def normalize_date_for_webflow(value: Any) -> Any:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # ISO 8601 and similar formats.
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

    # WordPress/RFC 2822:
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

    # Calendar date only.
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
    return read_csv_rows(csv_path)


class _LegacyWebflowClient:
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
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = f"{method} {url} failed: {exc}"

                if attempt >= self.max_retries:
                    raise MigrationError(last_error) from exc

                delay = min(2 ** attempt, 30)
                logging.warning(
                    "Network error. Retrying in %ss: %s",
                    delay,
                    exc,
                )
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
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else min(2 ** attempt, 30)
            )

            logging.warning(
                "Temporary Webflow error %s. Retrying in %ss...",
                response.status_code,
                delay,
            )
            time.sleep(delay)

        raise MigrationError(
            last_error or "Unknown Webflow request failure."
        )

    def get_collection_schema(
        self,
        collection_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_id = collection_id or self.collection_id
        response = self.request(
            "GET",
            f"{API_BASE}/collections/{target_id}",
        )
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
                    "Unexpected list-items response for collection "
                    f"{target_id}: {payload}"
                )

            items.extend(page_items)

            if len(page_items) < page_limit:
                break

            offset += page_limit

        return items

    def create_items(
        self,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not 1 <= len(items) <= 100:
            raise MigrationError(
                "Each Webflow batch must contain 1-100 items."
            )

        payload: Any = items[0] if len(items) == 1 else items

        response = self.request(
            "POST",
            f"{API_BASE}/collections/{self.collection_id}/items",
            expected=(200, 201, 202),
            params={"skipInvalidFiles": "false"},
            json=payload,
        )
        return response.json()

    def update_item(
        self,
        item_id: str,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update an existing draft CMS item."""

        response = self.request(
            "PATCH",
            f"{API_BASE}/collections/{self.collection_id}/items/{item_id}",
            expected=(200, 202),
            params={"skipInvalidFiles": "false"},
            json=item,
        )
        return response.json()

    def upload_asset(
        self,
        file_path: Path,
        alt_text: str = "",
        *,
        max_size_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> Dict[str, Any]:
        if not file_path.is_file():
            raise MigrationError(f"Image not found: {file_path}")

        if file_path.suffix.casefold() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise MigrationError(
                f"Unsupported image type: {file_path.name}"
            )

        size_bytes = file_path.stat().st_size
        if size_bytes > max_size_bytes:
            raise MigrationError(
                f"Image exceeds configured upload limit "
                f"({max_size_bytes / (1024 * 1024):.2f} MB): "
                f"{file_path}"
            )

        file_bytes = file_path.read_bytes()
        file_hash = hashlib.md5(file_bytes).hexdigest()

        metadata_response = self.request(
            "POST",
            f"{API_BASE}/sites/{self.site_id}/assets",
            expected=(200, 201, 202),
            json={
                "fileName": file_path.name[:99],
                "fileHash": file_hash,
            },
        )
        metadata = metadata_response.json()
        upload_url = metadata.get("uploadUrl")
        upload_details = metadata.get("uploadDetails")

        if not upload_url or not isinstance(upload_details, dict):
            raise MigrationError(
                "Webflow did not return upload details for "
                f"{file_path.name}: {metadata}"
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
                files={
                    "file": (
                        file_path.name,
                        file_bytes,
                        content_type,
                    )
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MigrationError(
                f"Asset upload failed for {file_path.name}: {exc}"
            ) from exc

        if upload_response.status_code not in (200, 201, 204):
            raise MigrationError(
                f"Storage upload failed for {file_path.name}: "
                f"{upload_response.status_code} "
                f"{upload_response.text[:2000]}"
            )

        file_id = metadata.get("id") or metadata.get("fileId")
        hosted_url = (
            metadata.get("hostedUrl")
            or metadata.get("assetUrl")
        )

        if not file_id or not hosted_url:
            raise MigrationError(
                "Missing file ID or hosted URL after uploading "
                f"{file_path.name}: {metadata}"
            )

        return {
            "fileId": file_id,
            "url": hosted_url,
            "alt": alt_text,
        }


def fields_by_slug(
    schema: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    return {
        str(field["slug"]): dict(field)
        for field in schema.get("fields", [])
        if isinstance(field, Mapping) and field.get("slug")
    }


def field_collection_id(
    field: Mapping[str, Any],
) -> Optional[str]:
    metadata = field.get("metadata")
    if isinstance(metadata, Mapping):
        for key in (
            "collectionId",
            "collectionID",
            "collection_id",
        ):
            value = metadata.get(key)
            if value:
                return str(value)

    validations = field.get("validations")
    if isinstance(validations, Mapping):
        for key in (
            "collectionId",
            "collectionID",
            "collection_id",
        ):
            value = validations.get(key)
            if value:
                return str(value)

    return None


def mapped_webflow_slugs(
    field_map: Mapping[str, Any],
) -> List[str]:
    result: List[str] = []

    for target in field_map.get("fields", {}).values():
        if isinstance(target, str) and target:
            result.append(target)
        elif isinstance(target, list):
            result.extend(
                str(item)
                for item in target
                if item
            )

    return result


def normalize_mapping_targets(target: Any) -> List[str]:
    if target is None:
        return []

    if isinstance(target, str):
        return [target] if target else []

    if isinstance(target, list):
        return [
            str(value)
            for value in target
            if value
        ]

    raise MigrationError(
        "Each field-map target must be null, a Webflow field slug "
        "string, or a list of slugs."
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
        return [
            "field-map.json must contain a 'fields' object."
        ]

    for csv_column, target in mappings.items():
        if csv_column not in csv_columns:
            errors.append(
                f"CSV column '{csv_column}' does not exist."
            )

        try:
            targets = normalize_mapping_targets(target)
        except MigrationError as exc:
            errors.append(f"CSV column '{csv_column}': {exc}")
            continue

        for webflow_slug in targets:
            if webflow_slug not in schema_fields:
                errors.append(
                    f"Webflow field slug '{webflow_slug}' mapped from "
                    f"'{csv_column}' does not exist in the "
                    "collection schema."
                )

    mapped = set(mapped_webflow_slugs(field_map))
    for required_slug in ("name", "slug"):
        if required_slug not in mapped:
            errors.append(
                f"Required Webflow field '{required_slug}' "
                "is not mapped."
            )

    image_config = field_map.get("images", {})

    for config_key, allowed_types in (
        ("main_image_field", IMAGE_TYPES),
        ("special_features_image_field", IMAGE_TYPES),
    ):
        slug = image_config.get(config_key)
        if not slug:
            continue

        field = schema_fields.get(slug)

        if not field:
            errors.append(
                f"Image field '{slug}' configured in "
                f"'{config_key}' does not exist."
            )
        elif field.get("type") not in allowed_types:
            errors.append(
                f"Image field '{slug}' is type "
                f"{field.get('type')}, expected one of "
                f"{sorted(allowed_types)}."
            )

    references = field_map.get("references", {})
    author_field = references.get("author_field", "author")
    tags_field = references.get("tags_field", "tags")

    if (
        author_field in schema_fields
        and schema_fields[author_field].get("type")
        not in REFERENCE_TYPES
    ):
        errors.append(
            f"Author field '{author_field}' is not a Reference field."
        )

    if (
        tags_field in schema_fields
        and schema_fields[tags_field].get("type")
        not in (REFERENCE_TYPES | MULTI_REFERENCE_TYPES)
    ):
        errors.append(
            f"Tags field '{tags_field}' is not a "
            "Reference/MultiReference field."
        )

    return errors


def build_reference_lookup(
    items: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    ambiguous: set[str] = set()

    for item in items:
        item_id = item.get("id")
        field_data = item.get("fieldData", {})

        if not item_id or not isinstance(field_data, Mapping):
            continue

        candidates = [
            field_data.get("name"),
            field_data.get("slug"),
        ]

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
            f"Could not resolve {label} reference for "
            f"'{row_slug}': CSV value={original!r}, "
            f"resolved value={resolved_name!r}."
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
        "author_aliases": normalized_aliases(
            config.get("author_aliases", {})
        ),
        "tag_aliases": normalized_aliases(
            config.get("tag_aliases", {})
        ),
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
                "Could not determine referenced collection ID for "
                f"{label} field '{slug}'. Inspect "
                "collection-schema.json and check the field metadata."
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


def transform_scalar(
    value: Any,
    field_type: str,
) -> Any:
    if value is None:
        return None

    if field_type in RICH_TEXT_TYPES:
        return plain_text_to_rich_text(value)

    if field_type == "DateTime":
        return normalize_date_for_webflow(value)

    if field_type == "Switch":
        lowered = normalize_lookup_key(value)

        if lowered in {
            "true",
            "yes",
            "1",
            "publish",
            "published",
        }:
            return True

        if lowered in {
            "false",
            "no",
            "0",
            "draft",
        }:
            return False

        raise MigrationError(
            f"Invalid Switch value: {value!r}"
        )

    if field_type == "Number":
        try:
            return float(str(value))
        except ValueError as exc:
            raise MigrationError(
                f"Invalid Number value: {value!r}"
            ) from exc

    return value


def strip_extraction_prefix(stem: str) -> str:
    return LEADING_SEQUENCE_PREFIX_RE.sub("", stem)


def parse_dimension_suffix(
    stem: str,
) -> Tuple[str, Optional[int], Optional[int], bool]:
    """Return base stem and dimensions parsed from a trailing WxH suffix."""

    match = DIMENSION_SUFFIX_RE.search(stem)

    if not match:
        return stem, None, None, False

    base = stem[:match.start()]
    width = int(match.group("width"))
    height = int(match.group("height"))

    return base, width, height, True


def logical_image_key(path: Path) -> str:
    """Create a grouping key for WordPress image variants.

    Examples
    --------
    021-1-thumbnail-1-575x1024.jpg
    022-1-thumbnail-1-862x1536.jpg
    023-1-thumbnail-1-1149x2048.jpg

    all become:

    1-thumbnail-1
    """

    stem = strip_extraction_prefix(path.stem)
    stem, _, _, _ = parse_dimension_suffix(stem)

    # WordPress sometimes produces a "-scaled" file for the same source.
    stem = WORDPRESS_GENERATED_SUFFIX_RE.sub("", stem)

    # Be conservative: normalize only case and separator runs.
    stem = stem.casefold().strip()
    stem = re.sub(r"[\s_]+", "-", stem)
    stem = re.sub(r"-+", "-", stem).strip("-")

    return stem


def read_local_image(
    path: Path,
    *,
    hero_pattern: re.Pattern[str],
) -> Optional[LocalImage]:
    """Read actual dimensions and filename metadata for one local image."""

    cleaned_stem = strip_extraction_prefix(path.stem)
    _, filename_width, filename_height, has_dimensions = (
        parse_dimension_suffix(cleaned_stem)
    )

    try:
        with Image.open(path) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logging.warning(
            "Skipping unreadable image %s: %s",
            path,
            exc,
        )
        return None

    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        logging.warning(
            "Could not inspect image %s: %s",
            path,
            exc,
        )
        return None

    return LocalImage(
        path=path,
        logical_key=logical_image_key(path),
        width=int(width),
        height=int(height),
        size_bytes=int(size_bytes),
        filename_width=filename_width,
        filename_height=filename_height,
        has_dimension_suffix=has_dimensions,
        is_hero=bool(hero_pattern.search(cleaned_stem)),
    )


def discover_local_images(
    images_root: Path,
    slug: str,
    *,
    hero_pattern_text: str,
) -> List[LocalImage]:
    folder = images_root / slug

    if not folder.is_dir():
        return []

    try:
        hero_pattern = re.compile(
            hero_pattern_text,
            flags=re.IGNORECASE,
        )
    except re.error as exc:
        raise MigrationError(
            f"Invalid images.hero_pattern regex: {exc}"
        ) from exc

    paths = sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file()
            and path.suffix.casefold()
            in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: str(
            path.relative_to(folder)
        ).casefold(),
    )

    images = [
        image
        for path in paths
        if (
            image := read_local_image(
                path,
                hero_pattern=hero_pattern,
            )
        )
        is not None
    ]

    return images


def variant_quality_key(
    image: LocalImage,
) -> Tuple[int, int, int, str]:
    """Rank duplicate variants.

    Preference order is pixel area, width, file size, then path.
    """

    return (
        -image.pixel_area,
        -image.width,
        -image.size_bytes,
        str(image.path).casefold(),
    )


def select_best_image_variants(
    images: Sequence[LocalImage],
    *,
    max_size_bytes: int,
) -> List[LocalImage]:
    """Keep only the best-resolution uploadable image in each family.

    A filename with trailing dimensions is treated as evidence that WordPress
    generated multiple variants of the same logical image. Variants are grouped
    after removing:
    - the extraction sequence prefix, such as "023-"
    - the trailing WordPress dimensions, such as "-1149x2048"

    An undimensioned file sharing the same logical key is also compared using
    its actual Pillow dimensions.
    """

    by_key: Dict[str, List[LocalImage]] = defaultdict(list)

    for image in images:
        by_key[image.logical_key].append(image)

    selected: List[LocalImage] = []

    for logical_key, variants in sorted(by_key.items()):
        uploadable = [
            image
            for image in variants
            if image.size_bytes <= max_size_bytes
        ]

        if not uploadable:
            largest = sorted(variants, key=variant_quality_key)[0]

            logging.warning(
                "Skipping image family '%s': all %s variants exceed "
                "the configured %.2f MB upload limit. Largest candidate: "
                "%s (%sx%s, %.2f MB).",
                logical_key,
                len(variants),
                max_size_bytes / (1024 * 1024),
                largest.path.name,
                largest.width,
                largest.height,
                largest.size_bytes / (1024 * 1024),
            )
            continue

        best = sorted(uploadable, key=variant_quality_key)[0]
        selected.append(best)

        if len(variants) > 1:
            discarded = [
                image
                for image in variants
                if image.path != best.path
            ]

            logging.info(
                "Image family '%s': keeping %s (%sx%s, %.1f KB); "
                "skipping %s lower-resolution/duplicate variant(s).",
                logical_key,
                best.path.name,
                best.width,
                best.height,
                best.size_bytes / 1024,
                len(discarded),
            )

            if logging.getLogger().isEnabledFor(logging.DEBUG):
                for image in sorted(
                    discarded,
                    key=variant_quality_key,
                ):
                    logging.debug(
                        "Skipped variant: %s (%sx%s, %.1f KB)",
                        image.path.name,
                        image.width,
                        image.height,
                        image.size_bytes / 1024,
                    )

    return sorted(
        selected,
        key=lambda image: image.path.name.casefold(),
    )


def choose_hero_image(
    images: Sequence[LocalImage],
    *,
    slug: str,
    folder: Path,
) -> LocalImage:
    hero_candidates = [
        image
        for image in images
        if image.is_hero
    ]

    if not hero_candidates:
        raise MigrationError(
            f"No G[number] Hero Image found for '{slug}' in {folder}. "
            "Expected a filename such as '099-G3-Icon.png'."
        )

    # If the hero itself has multiple resize variants, the earlier grouping
    # already reduced the family to its best-quality copy. This fallback picks
    # the highest-quality one if multiple distinct G<number> files remain.
    hero = sorted(hero_candidates, key=variant_quality_key)[0]

    if len(hero_candidates) > 1:
        logging.warning(
            "Found %s distinct G[number] Hero Image candidates for '%s'. "
            "Using the highest-resolution candidate: %s.",
            len(hero_candidates),
            slug,
            hero.path.name,
        )

    return hero


def select_gallery_images(
    images: Sequence[LocalImage],
    *,
    hero: LocalImage,
    special_features_image: Optional[LocalImage] = None,
) -> List[LocalImage]:
    return exclude_reserved_images_from_gallery(
        images,
        hero=hero,
        special_features_image=special_features_image,
    )


def find_special_features_image(
    images: Sequence[LocalImage],
    filename: str,
) -> List[LocalImage]:
    """Find the configured filename and all WordPress resize variants."""

    family = [
        image for image in images
        if matches_special_features_picture(image.path, filename)
    ]
    if len(family) > 1:
        logging.warning(
            "Multiple Special Features Picture candidates found: %s",
            ", ".join(str(image.path) for image in family),
        )
    return family


def select_special_features_variant(
    discovered_family: Sequence[LocalImage],
    deduplicated_images: Sequence[LocalImage],
    *,
    slug: str,
) -> Optional[LocalImage]:
    if not discovered_family:
        logging.info("No Special Features Picture found for %s.", slug)
        return None

    family_keys = {image.logical_key for image in discovered_family}
    candidates = [
        image for image in deduplicated_images
        if image.logical_key in family_keys
    ]
    if not candidates:
        logging.info("No Special Features Picture found for %s.", slug)
        return None

    selected = sorted(candidates, key=variant_quality_key)[0]
    logging.info(
        "Special Features Picture for %s: selected %s (%sx%s).",
        slug,
        selected.path.name,
        selected.width,
        selected.height,
    )
    for image in sorted(
        (item for item in discovered_family if item.path != selected.path),
        key=variant_quality_key,
    ):
        logging.info(
            "Special Features Picture for %s: skipped variant %s (%sx%s).",
            slug,
            image.path.name,
            image.width,
            image.height,
        )
    return selected


def exclude_reserved_images_from_gallery(
    images: Sequence[LocalImage],
    *,
    hero: LocalImage,
    special_features_image: Optional[LocalImage],
) -> List[LocalImage]:
    reserved_keys = {hero.logical_key}
    if special_features_image is not None:
        reserved_keys.add(special_features_image.logical_key)

    gallery = [
        image
        for image in images
        if image.logical_key not in reserved_keys
        and not image.is_hero
    ]

    return sorted(
        gallery,
        key=lambda image: image.path.name.casefold(),
    )


def find_images_for_slug(
    images_root: Path,
    slug: str,
    *,
    hero_pattern_text: str = DEFAULT_HERO_PATTERN,
    max_size_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    special_features_filename: Optional[str] = None,
) -> Tuple[Optional[LocalImage], Optional[LocalImage], List[LocalImage]]:
    images = discover_local_images(
        images_root,
        slug,
        hero_pattern_text=hero_pattern_text,
    )

    if not images:
        return None, None, []

    # Reserved-image families are identified before variant deduplication.
    special_family = (
        find_special_features_image(images, special_features_filename)
        if special_features_filename
        else []
    )

    best_variants = select_best_image_variants(
        images,
        max_size_bytes=max_size_bytes,
    )

    if not best_variants:
        return None, None, []

    folder = images_root / slug
    hero = choose_hero_image(
        best_variants,
        slug=slug,
        folder=folder,
    )
    special_features_image = select_special_features_variant(
        special_family,
        best_variants,
        slug=slug,
    ) if special_features_filename else None
    gallery = select_gallery_images(
        best_variants,
        hero=hero,
        special_features_image=special_features_image,
    )

    return hero, special_features_image, gallery


def image_cache_key(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.md5(path.read_bytes()).hexdigest()

    return (
        f"{path.resolve()}::{stat.st_size}::{digest}"
    )


def upload_one_local_image(
    client: WebflowClient,
    image: LocalImage | ProcessedImage,
    *,
    alt_text: str,
    checkpoint: Dict[str, Any],
    checkpoint_path: Path,
    dry_run: bool,
    max_size_bytes: int,
) -> Dict[str, Any]:
    cache_key = image_cache_key(image.path)
    cached = checkpoint["assets"].get(cache_key)

    if not cached:
        content_signature = "::".join(cache_key.rsplit("::", 2)[-2:])
        for existing_key, existing_asset in checkpoint["assets"].items():
            if str(existing_key).endswith(f"::{content_signature}"):
                cached = existing_asset
                checkpoint["assets"][cache_key] = existing_asset
                checkpoint.setdefault("reused_assets", {})[cache_key] = {
                    "source": existing_key,
                    "path": str(image.path),
                }
                save_json(checkpoint_path, checkpoint)
                logging.info(
                    "Reusing uploaded asset by file content: %s",
                    image.path.name,
                )
                break

    if cached:
        logging.info(
            "Reusing uploaded asset: %s",
            image.path.name,
        )
        return cached

    if dry_run:
        return {
            "fileId": (
                "DRY_RUN_"
                + hashlib.md5(
                    str(image.path).encode("utf-8")
                ).hexdigest()[:12]
            ),
            "url": (
                "https://example.invalid/"
                + image.path.name
            ),
            "alt": alt_text,
        }

    logging.info(
        "Uploading image: %s (%sx%s, %.1f KB)",
        image.path,
        image.width,
        image.height,
        image.size_bytes / 1024,
    )

    asset = client.upload_asset(
        image.path,
        alt_text=alt_text,
        max_size_bytes=max_size_bytes,
    )
    checkpoint["assets"][cache_key] = asset
    save_json(checkpoint_path, checkpoint)

    return asset


def upload_images_for_row(
    client: WebflowClient,
    row: Dict[str, Any],
    field_map: Dict[str, Any],
    schema: Dict[str, Any],
    checkpoint: Dict[str, Any],
    checkpoint_path: Path,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    config = field_map.get("images", {})
    root_value = config.get("root")
    main_field = config.get("main_image_field")
    detected_special_field = find_special_features_picture_field(
        schema.get("fields", [])
    )
    special_features_field = (
        detected_special_field.get("slug")
        if detected_special_field
        else config.get("special_features_image_field")
    )
    if not root_value or not (main_field or special_features_field):
        return {}

    slug = str(row.get("slug") or "").strip()
    title = str(row.get("title") or slug)

    images_root = Path(str(root_value))
    hero_pattern_text = str(
        config.get(
            "hero_pattern",
            DEFAULT_HERO_PATTERN,
        )
    )
    special_features_filename = (
        str(
            config.get(
                "special_features_filename",
                DEFAULT_SPECIAL_FEATURES_FILENAME,
            )
        )
        if special_features_field
        else None
    )
    max_size_bytes = int(
        config.get(
            "max_image_bytes",
            DEFAULT_MAX_IMAGE_BYTES,
        )
    )
    if max_size_bytes < 1:
        raise MigrationError(
            "images.max_image_bytes must be at least 1."
        )

    hero, special_features_image, gallery_images = find_images_for_slug(
        images_root,
        slug,
        hero_pattern_text=hero_pattern_text,
        max_size_bytes=max_size_bytes,
        special_features_filename=special_features_filename,
    )

    if hero is None:
        message = (
            f"No usable images found for slug '{slug}' in "
            f"{images_root / slug}."
        )

        if config.get("required", False):
            raise MigrationError(message)

        logging.warning(message)
        return {}

    logging.info("Gallery: %s", slug)
    logging.info(
        "Ordinary photos after hero/special exclusion and deduplication: %s",
        len(gallery_images),
    )
    logging.info(
        "Ordinary photos for %s will be migrated only as Gallery Photos items.",
        slug,
    )

    logging.info(
        "Hero image for %s: %s (%sx%s)",
        slug,
        hero.path.name,
        hero.width,
        hero.height,
    )

    logging.info("Special image selection for %s:", slug)
    logging.info("- Hero Image: %s", hero.path.name)
    logging.info(
        "- Special Features Picture: %s",
        special_features_image.path.name
        if special_features_image is not None
        else "empty",
    )

    hero_asset = upload_one_local_image(
        client,
        hero,
        alt_text=f"{title} - Hero Image",
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        dry_run=dry_run,
        max_size_bytes=max_size_bytes,
    )

    special_features_asset: Optional[Dict[str, Any]] = None
    if special_features_field and special_features_image is not None:
        special_features_asset = upload_one_local_image(
            client,
            special_features_image,
            alt_text=f"{title} - Special Features Picture",
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            dry_run=dry_run,
            max_size_bytes=max_size_bytes,
        )

    result: Dict[str, Any] = {}

    if main_field:
        result[str(main_field)] = hero_asset

    if special_features_field and special_features_asset is not None:
        result[str(special_features_field)] = special_features_asset

    logging.info(
        "TOD Gallery payload for %s contains only hero/special images; "
        "%s ordinary photo(s) were excluded.",
        slug,
        len(gallery_images),
    )

    return result


def load_checkpoint(path: Path) -> Dict[str, Any]:
    return load_checkpoint_state(path)


def build_webflow_item(
    row: Dict[str, Any],
    field_map: Dict[str, Any],
    schema: Dict[str, Any],
    reference_context: Dict[str, Any],
    image_fields: Dict[str, Any],
) -> Dict[str, Any]:
    schema_fields = fields_by_slug(schema)
    reference_config = field_map.get("references", {})
    author_csv_column = str(
        reference_config.get(
            "author_csv_column",
            "author",
        )
    )
    tags_csv_column = str(
        reference_config.get(
            "tags_csv_column",
            "tags",
        )
    )
    author_field = str(
        reference_context.get(
            "author_field",
            "author",
        )
    )
    tags_field = str(
        reference_context.get(
            "tags_field",
            "tags",
        )
    )
    row_slug = str(row.get("slug") or "").strip()

    field_data: Dict[str, Any] = {}

    for csv_column, target in field_map.get(
        "fields",
        {},
    ).items():
        targets = normalize_mapping_targets(target)

        if not targets:
            continue

        value = row.get(csv_column)
        if value is None:
            continue

        for webflow_slug in targets:
            if webflow_slug in {
                author_field,
                tags_field,
            }:
                continue

            field = schema_fields.get(
                webflow_slug,
                {},
            )
            transformed = transform_scalar(
                value,
                str(field.get("type", "")),
            )

            if transformed is not None:
                field_data[webflow_slug] = transformed

    author_value = row.get(author_csv_column)

    if (
        author_value is not None
        and author_field in schema_fields
    ):
        author_id = resolve_reference_name(
            author_value,
            reference_context.get(
                "author_lookup",
                {},
            ),
            reference_context.get(
                "author_aliases",
                {},
            ),
            label="Author",
            row_slug=row_slug,
        )

        if author_id:
            field_data[author_field] = author_id

    tag_values = split_multi_value(
        row.get(tags_csv_column)
    )

    if tag_values and tags_field in schema_fields:
        tag_ids: List[str] = []

        for tag in tag_values:
            tag_id = resolve_reference_name(
                tag,
                reference_context.get(
                    "tags_lookup",
                    {},
                ),
                reference_context.get(
                    "tag_aliases",
                    {},
                ),
                label="Tag",
                row_slug=row_slug,
            )

            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)

        tags_type = schema_fields[tags_field].get(
            "type"
        )

        if tags_type in MULTI_REFERENCE_TYPES:
            field_data[tags_field] = tag_ids

        elif tags_type in REFERENCE_TYPES:
            if len(tag_ids) > 1:
                raise MigrationError(
                    f"Tags field '{tags_field}' is a single "
                    f"Reference but '{row_slug}' contains "
                    f"{len(tag_ids)} tags."
                )

            if tag_ids:
                field_data[tags_field] = tag_ids[0]

    parent_gallery_photo_fields = {
        str(field.get("slug") or "")
        for field in schema.get("fields", [])
        if re.fullmatch(
            r"Gallery Images(?: \d+)?",
            str(field.get("displayName") or "").strip(),
            flags=re.IGNORECASE,
        )
    }
    forbidden = parent_gallery_photo_fields.intersection(image_fields)
    if forbidden:
        raise MigrationError(
            "Ordinary photos cannot be included in a TOD Gallery payload: "
            + ", ".join(sorted(forbidden))
        )
    field_data.update(image_fields)

    if not field_data.get("name"):
        raise MigrationError(
            f"CSV row {row['_source_row']} has no mapped "
            "Webflow name."
        )

    if not field_data.get("slug"):
        raise MigrationError(
            f"CSV row {row['_source_row']} has no mapped "
            "Webflow slug."
        )

    return {
        "isArchived": False,
        "isDraft": True,
        "fieldData": field_data,
    }


def extract_created_items(
    payload: Any,
) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [
            item
            for item in payload
            if isinstance(item, dict)
        ]

    if not isinstance(payload, dict):
        return []

    if isinstance(payload.get("items"), list):
        return [
            item
            for item in payload["items"]
            if isinstance(item, dict)
        ]

    if payload.get("id"):
        return [payload]

    return []


def command_inspect(
    client: WebflowClient,
    schema_path: Path,
) -> None:
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
            f"- {field.get('displayName')} | "
            f"slug={field.get('slug')} | "
            f"type={field.get('type')} | "
            f"required={field.get('isRequired')} | "
            "referenceCollection="
            f"{field_collection_id(field) or '-'}"
        )


def validate_rows(
    rows: Sequence[Dict[str, Any]],
) -> List[str]:
    errors: List[str] = []
    slugs = [
        str(row.get("slug") or "").strip()
        for row in rows
    ]

    duplicate_slugs = sorted(
        {
            slug
            for slug in slugs
            if slug and slugs.count(slug) > 1
        }
    )

    if duplicate_slugs:
        errors.append(
            "Duplicate CSV slugs: "
            + ", ".join(duplicate_slugs)
        )

    blank_rows = [
        str(row["_source_row"])
        for row in rows
        if not str(row.get("slug") or "").strip()
    ]

    if blank_rows:
        errors.append(
            "Rows with blank slugs: "
            + ", ".join(blank_rows)
        )

    unexpected = sorted(
        {
            slug
            for slug in slugs
            if slug and slug not in APPROVED_SLUGS
        }
    )

    if unexpected:
        errors.append(
            "CSV contains unapproved gallery slugs: "
            + ", ".join(unexpected)
        )

    missing = sorted(
        APPROVED_SLUGS - set(slugs)
    )

    if missing:
        errors.append(
            "CSV is missing approved gallery slugs: "
            + ", ".join(missing)
        )

    return errors


def command_validate(
    client: WebflowClient,
    rows: List[Dict[str, Any]],
    schema: Dict[str, Any],
    field_map: Dict[str, Any],
) -> Dict[str, Any]:
    if not rows:
        raise MigrationError(
            "No CSV records were found."
        )

    columns = set(rows[0].keys()) - {
        "_source_row"
    }
    errors = validate_field_map(
        field_map,
        columns,
        schema,
    )
    errors.extend(validate_rows(rows))

    reference_context: Dict[str, Any] = {}

    if not errors:
        try:
            reference_context = prepare_reference_context(
                client,
                schema,
                field_map,
            )

            image_config = field_map.get(
                "images",
                {},
            )
            image_root = Path(
                str(image_config.get("root", ""))
            )
            hero_pattern_text = str(
                image_config.get(
                    "hero_pattern",
                    DEFAULT_HERO_PATTERN,
                )
            )
            special_features_field = image_config.get(
                "special_features_image_field"
            )
            special_features_filename = (
                str(
                    image_config.get(
                        "special_features_filename",
                        DEFAULT_SPECIAL_FEATURES_FILENAME,
                    )
                )
                if special_features_field
                else None
            )
            max_size_bytes = int(
                image_config.get(
                    "max_image_bytes",
                    DEFAULT_MAX_IMAGE_BYTES,
                )
            )
            for row in rows:
                slug = str(
                    row.get("slug") or ""
                ).strip()

                author_column = field_map.get(
                    "references",
                    {},
                ).get(
                    "author_csv_column",
                    "author",
                )
                author_value = row.get(author_column)

                if author_value:
                    resolve_reference_name(
                        author_value,
                        reference_context.get(
                            "author_lookup",
                            {},
                        ),
                        reference_context.get(
                            "author_aliases",
                            {},
                        ),
                        label="Author",
                        row_slug=slug,
                    )

                tags_column = field_map.get(
                    "references",
                    {},
                ).get(
                    "tags_csv_column",
                    "tags",
                )

                for tag in split_multi_value(
                    row.get(tags_column)
                ):
                    resolve_reference_name(
                        tag,
                        reference_context.get(
                            "tags_lookup",
                            {},
                        ),
                        reference_context.get(
                            "tag_aliases",
                            {},
                        ),
                        label="Tag",
                        row_slug=slug,
                    )

                if image_config.get("required"):
                    hero, _, gallery_images = find_images_for_slug(
                        image_root,
                        slug,
                        hero_pattern_text=hero_pattern_text,
                        max_size_bytes=max_size_bytes,
                        special_features_filename=special_features_filename,
                    )

                    if hero is None:
                        errors.append(
                            f"No required usable images found for "
                            f"'{slug}' in {image_root / slug}."
                        )
                    else:
                        logging.info(
                            "Validated %s ordinary photo(s) for %s; they will "
                            "not be written to TOD Gallery multi-image fields.",
                            len(gallery_images),
                            slug,
                        )

        except MigrationError as exc:
            errors.append(str(exc))

    if errors:
        print("\nValidation failed:")

        for error in errors:
            print(f"- {error}")

        raise MigrationError(
            f"Validation found {len(errors)} problem(s)."
        )

    print(
        f"Validation passed: {len(rows)} CSV records, "
        f"{len(schema.get('fields', []))} Webflow fields, "
        f"{len(reference_context.get('author_lookup', {}))} "
        "Author lookup entries, and "
        f"{len(reference_context.get('tags_lookup', {}))} "
        "Tag lookup entries."
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
        logging.info(
            "Reading existing Webflow items to prevent "
            "duplicate slugs..."
        )

        existing_slugs = {
            str(
                item.get(
                    "fieldData",
                    {},
                ).get(
                    "slug",
                )
                or ""
            ).strip()
            for item in client.list_items()
        }

        logging.info(
            "Found %s existing Webflow slugs.",
            len(existing_slugs),
        )

    selected_rows = list(rows)

    if selected_slug:
        normalized_selected = normalize_slug(
            selected_slug
        )
        selected_rows = [
            row
            for row in selected_rows
            if normalize_slug(
                row.get("slug")
            )
            == normalized_selected
        ]

        if not selected_rows:
            raise MigrationError(
                "No CSV record found for --slug "
                f"{selected_slug!r}."
            )

    if start_row is not None:
        selected_rows = [
            row
            for row in selected_rows
            if row["_source_row"] >= start_row
        ]

    pending: List[Dict[str, Any]] = []

    for row in selected_rows:
        slug = str(
            row.get("slug") or ""
        ).strip()

        completed_entry = checkpoint["completed"].get(slug)
        completed_status = (
            str(completed_entry.get("status") or "")
            if isinstance(completed_entry, Mapping)
            else ""
        )
        if completed_entry and (dry_run or completed_status != "dry_run"):
            logging.info(
                "Skipping completed slug: %s",
                slug,
            )
            continue

        if slug in existing_slugs:
            logging.info(
                "Skipping slug already in Webflow: %s",
                slug,
            )
            checkpoint["skipped_existing"][slug] = {
                "status": "already_exists",
                "sourceRow": row["_source_row"],
            }
            save_json(
                checkpoint_path,
                checkpoint,
            )
            continue

        pending.append(row)

    if limit is not None:
        pending = pending[:limit]

    if not pending:
        logging.info("Nothing to migrate.")
        return

    run_results: List[Dict[str, Any]] = []
    logs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for row_batch in chunks(
        pending,
        batch_size,
    ):
        request_items: List[Dict[str, Any]] = []
        batch_rows: List[Dict[str, Any]] = []

        for row in row_batch:
            slug = str(
                row.get("slug") or ""
            ).strip()

            try:
                image_fields = upload_images_for_row(
                    client,
                    row,
                    field_map,
                    schema,
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

                payload_path = (
                    logs_dir
                    / f"{slug}-payload.json"
                )
                save_json(payload_path, item)

                logging.info(
                    "Final payload saved: %s",
                    payload_path,
                )

                if logging.getLogger().isEnabledFor(
                    logging.DEBUG
                ):
                    logging.debug(
                        "Payload for %s:\n%s",
                        slug,
                        json.dumps(
                            item,
                            indent=2,
                            ensure_ascii=False,
                        ),
                    )

                request_items.append(item)
                batch_rows.append(row)

            except Exception as exc:
                logging.exception(
                    "Could not prepare CSV row %s (%s).",
                    row["_source_row"],
                    slug,
                )
                checkpoint["failed"][
                    slug
                    or f"row-{row['_source_row']}"
                ] = {
                    "sourceRow": row["_source_row"],
                    "error": str(exc),
                }
                save_json(
                    checkpoint_path,
                    checkpoint,
                )

        if not request_items:
            continue

        if dry_run:
            response_payload: Any = {
                "items": [
                    {
                        "id": (
                            "DRY_RUN_"
                            + str(row.get("slug"))
                        ),
                        **item,
                    }
                    for row, item in zip(
                        batch_rows,
                        request_items,
                    )
                ]
            }
        else:
            logging.info(
                "Creating Webflow batch of %s "
                "draft item(s)...",
                len(request_items),
            )
            response_payload = client.create_items(
                request_items
            )

        created_items = extract_created_items(
            response_payload
        )

        for index, row in enumerate(batch_rows):
            slug = str(
                row.get("slug") or ""
            ).strip()

            response_for_item = (
                created_items[index]
                if index < len(created_items)
                else response_payload
            )

            save_json(
                logs_dir
                / f"{slug}-response.json",
                response_for_item,
            )

            created = (
                created_items[index]
                if index < len(created_items)
                else {}
            )

            result = {
                "slug": slug,
                "sourceRow": row["_source_row"],
                "webflowItemId": created.get("id"),
                "isDraft": created.get(
                    "isDraft",
                    request_items[index].get(
                        "isDraft"
                    ),
                ),
                "status": (
                    "dry_run"
                    if dry_run
                    else "created"
                ),
            }

            checkpoint["completed"][slug] = result
            checkpoint["failed"].pop(
                slug,
                None,
            )
            run_results.append(result)

        save_json(
            checkpoint_path,
            checkpoint,
        )
        save_json(
            results_path,
            run_results,
        )

        logging.info(
            "Finished batch. Completed in this run: %s",
            len(run_results),
        )

    logging.info(
        "Migration finished. Results: %s | "
        "Checkpoint: %s | Logs: %s",
        results_path,
        checkpoint_path,
        logs_dir,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate Treasury of Discoveries CSV content "
            "into Webflow CMS."
        )
    )

    parser.add_argument(
        "command",
        choices=(
            "inspect",
            "validate",
            "dry-run",
            "migrate",
            "photo-sets",
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For photo-sets, report all actions without mutating Webflow or .env.",
    )
    parser.add_argument(
        "--csv",
        default="tod.csv",
    )
    parser.add_argument(
        "--field-map",
        default="field-map.json",
    )
    parser.add_argument(
        "--schema-output",
        default="collection-schema.json",
    )
    parser.add_argument(
        "--checkpoint",
        default="migration-checkpoint.json",
    )
    parser.add_argument(
        "--results",
        default="migration-results.json",
    )
    parser.add_argument(
        "--logs-dir",
        default="migration-logs",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--slug",
        default=None,
        help=(
            "Process only one exact gallery slug."
        ),
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--allow-existing-slugs",
        action="store_true",
        help=(
            "Skip the Webflow duplicate-slug query. "
            "Not recommended."
        ),
    )
    parser.add_argument(
        "--refresh-schema",
        action="store_true",
        help=(
            "Fetch collection-schema.json again before "
            "validate/dry-run/migrate."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("galleries", "photos", "all"),
        default="all",
        help=(
            "Migrate parent galleries, child photos, or both. "
            "--limit applies independently to each selected scope."
        ),
    )
    parser.add_argument(
        "--photos-csv",
        default=str(DEFAULT_GALLERY_PHOTOS_CSV),
    )
    parser.add_argument(
        "--photos-schema-output",
        default="gallery-photos-schema.json",
    )
    parser.add_argument(
        "--photos-field-map",
        default="gallery-photos-field-map.json",
    )
    parser.add_argument(
        "--allow-photo-upscaling",
        action="store_true",
        help=(
            "Explicitly allow smaller child photos to be enlarged to "
            "1600x1200. Disabled by default."
        ),
    )
    parser.add_argument(
        "--update-existing-photos",
        action="store_true",
        help="Update existing child items by slug instead of skipping them.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logs_dir = Path(args.logs_dir)

    setup_logging(
        args.verbose,
        logs_dir / "migration.log",
    )
    load_dotenv()

    token = os.getenv(
        "WEBFLOW_API_TOKEN",
        "",
    ).strip()
    site_id = os.getenv(
        "WEBFLOW_SITE_ID",
        "",
    ).strip()
    collection_id = (
        os.getenv("TOD_GALLERIES_COLLECTION_ID", "").strip()
        or os.getenv("WEBFLOW_COLLECTION_ID", "").strip()
    )
    photos_collection_id = (
        os.getenv("GALLERY_PHOTOS_COLLECTION_ID", "").strip()
        or os.getenv(
            "TOD_GALLERY_PHOTOS_COLLECTION_ID",
            "6a667932b6dcc4a8a905e336",
        ).strip()
    )

    missing = [
        name
        for name, value in (
            ("WEBFLOW_API_TOKEN", token),
            ("WEBFLOW_SITE_ID", site_id),
            ("TOD_GALLERIES_COLLECTION_ID/WEBFLOW_COLLECTION_ID", collection_id),
            (
                "GALLERY_PHOTOS_COLLECTION_ID/"
                "TOD_GALLERY_PHOTOS_COLLECTION_ID",
                photos_collection_id,
            ),
        )
        if not value
    ]

    if missing:
        raise MigrationError(
            "Missing environment variables: "
            + ", ".join(missing)
        )

    if not 1 <= args.batch_size <= 100:
        raise MigrationError(
            "--batch-size must be between 1 and 100."
        )

    if args.limit is not None and args.limit < 1:
        raise MigrationError(
            "--limit must be at least 1."
        )

    if args.command == "photo-sets":
        summary = run_photo_sets(
            WebflowClient(token, site_id, collection_id),
            parent_collection_id=collection_id,
            photos_collection_id=photos_collection_id,
            configured_photo_sets_id=os.getenv(
                "TOD_PHOTO_SETS_COLLECTION_ID", ""
            ).strip(),
            dry_run=args.dry_run,
            limit=args.limit,
            env_path=Path(".env"),
        )
        logging.info(
            "Photo Sets: inspected=%s unique=%s create=%s reuse=%s "
            "link=%s already-linked=%s missing-parent=%s invalid=%s "
            "singletons=%s singleton-percent=%s largest-set=%s ambiguous=%s",
            summary["photos_inspected"], summary["unique_sets"],
            summary["sets_created"], summary["sets_reused"],
            summary["photos_linked"], summary["already_linked"],
            summary["missing_parent_count"], len(summary["invalid_filenames"]),
            summary["group_size_summary"]["single_photo_sets"],
            summary["group_size_summary"]["singleton_percentage"],
            summary["group_size_summary"]["largest_set_size"],
            len(summary["ambiguous_groups"]),
        )
        if args.dry_run:
            logging.info(
                "Review exports: full=%s singletons=%s total_sets=%s "
                "singleton_count=%s low_confidence_count=%s warning_count=%s",
                summary["review_csv"], summary["singletons_review_csv"],
                summary["unique_sets"],
                summary["group_size_summary"]["single_photo_sets"],
                summary["low_confidence_count"], summary["warning_count"],
            )
        return 0

    client = WebflowClient(
        token,
        site_id,
        collection_id,
    )
    photos_client = WebflowClient(
        token,
        site_id,
        photos_collection_id,
    )
    schema_path = Path(
        args.schema_output
    )

    if args.command == "inspect":
        if args.scope in {"galleries", "all"}:
            command_inspect(client, schema_path)
        if args.scope in {"photos", "all"}:
            command_inspect(
                photos_client,
                Path(args.photos_schema_output),
            )
        return 0

    rows = load_csv_rows(Path(args.csv))

    if (
        args.refresh_schema
        or not schema_path.exists()
    ):
        logging.info(
            "Retrieving the latest Webflow "
            "collection schema..."
        )
        save_json(
            schema_path,
            client.get_collection_schema(),
        )

    schema = load_json(schema_path)
    field_map = load_json(
        Path(args.field_map)
    )
    photos_schema_path = Path(args.photos_schema_output)
    if (
        args.refresh_schema
        or not photos_schema_path.exists()
    ) and args.scope in {"photos", "all"}:
        logging.info("Retrieving Gallery Photos schema...")
        save_json(
            photos_schema_path,
            photos_client.get_collection_schema(),
        )
    photos_schema = (
        load_json(photos_schema_path)
        if args.scope in {"photos", "all"}
        else {}
    )

    reference_context: Dict[str, Any] = {}
    if args.scope in {"galleries", "all"}:
        reference_context = command_validate(
            client,
            rows,
            schema,
            field_map,
        )

    photo_records = []
    if args.scope in {"photos", "all"}:
        photos_csv_path = Path(args.photos_csv)
        if (
            photos_csv_path == DEFAULT_GALLERY_PHOTOS_CSV
            and not photos_csv_path.is_file()
            and LEGACY_GALLERY_PHOTOS_CSV.is_file()
        ):
            logging.warning(
                "%s is missing; using backward-compatible photo CSV %s.",
                photos_csv_path,
                LEGACY_GALLERY_PHOTOS_CSV,
            )
            photos_csv_path = LEGACY_GALLERY_PHOTOS_CSV
        photo_records = load_gallery_photo_rows(photos_csv_path)
        parent_slugs = {
            str(row.get("slug") or "").strip() for row in rows
        }
        photo_errors = validate_child_schema(
            photos_schema,
            collection_id,
        )
        row_errors, photo_warnings = validate_gallery_photo_rows(
            photo_records,
            parent_slugs,
        )
        photo_errors.extend(row_errors)
        for warning in photo_warnings:
            logging.warning("%s", warning)
        if photo_errors:
            for error in photo_errors:
                logging.error("%s", error)
            raise MigrationError(
                f"Photo validation found {len(photo_errors)} problem(s)."
            )
        logging.info(
            "Photo validation passed: %s records, %s descriptions, "
            "%s destination URLs, %s warning(s).",
            len(photo_records),
            sum(bool(record.description) for record in photo_records),
            sum(bool(record.destination_url) for record in photo_records),
            len(photo_warnings),
        )

    if args.command == "validate":
        return 0

    checkpoint_path = Path(args.checkpoint)
    dry_run = args.command == "dry-run"
    if args.scope in {"galleries", "all"}:
        command_migrate(
            client,
            rows,
            schema,
            field_map,
            reference_context,
            checkpoint_path,
            Path(args.results),
            logs_dir,
            batch_size=args.batch_size,
            limit=args.limit,
            selected_slug=args.slug,
            start_row=args.start_row,
            dry_run=dry_run,
            skip_existing_webflow_slugs=(
                not args.allow_existing_slugs
            ),
        )

    if args.scope in {"photos", "all"}:
        checkpoint = load_checkpoint(checkpoint_path)
        parent_ids = {
            normalize_gallery_slug(
                str(item.get("fieldData", {}).get("slug") or "")
            ): str(item.get("id") or "")
            for item in client.list_items()
        }
        parent_ids.update(
            {
                normalize_gallery_slug(slug): str(
                    value.get("webflowItemId") or ""
                )
                for slug, value in checkpoint["completed"].items()
                if value.get("webflowItemId")
                and value.get("status") != "dry_run"
                and not str(value.get("webflowItemId")).startswith("DRY_RUN_")
            }
        )
        selected_photos = (
            [
                record
                for record in photo_records
                if record.gallery_slug == normalize_slug(args.slug)
            ]
            if args.slug
            else photo_records
        )
        summary = migrate_gallery_photos(
            client=photos_client,
            records=selected_photos,
            schema=photos_schema,
            parent_ids=parent_ids,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            processed_root=PROCESSED_IMAGES_DIRECTORY,
            upload_image=upload_one_local_image,
            save_checkpoint=save_json,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=dry_run,
            allow_upscaling=args.allow_photo_upscaling,
            update_existing=args.update_existing_photos,
        )
        logging.info(
            "Gallery Photos created: %s | updated: %s | "
            "skipped: %s | failed: %s",
            summary["created"],
            summary["updated"],
            summary["skipped"],
            summary["failed"],
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.error(
            "Migration interrupted by user."
        )
        raise SystemExit(130)
    except MigrationError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
