"""Extract one structured record per clickable TOD gallery photo."""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .description_parser import (
    html_to_description,
    normalize_description,
    normalize_text_fragment,
)
from .images import (
    is_hero_image,
    is_special_features_picture,
    logical_image_key,
    parse_dimension_suffix,
    read_image_candidate,
    select_highest_resolution_image,
)
from .models import GalleryPhotoRecord, ImageCandidate
from .photo_description_parser import enrich_photo_record
from .xml_parser import WordPressItem, parse_wordpress_items

LOGGER = logging.getLogger(__name__)
APPROVED_GALLERY_SLUGS = {
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
PHOTO_CSV_FIELDS = [
    "name",
    "slug",
    "gallery_slug",
    "image_filename",
    "image_path",
    "image_url",
    "destination_url",
    "alt_text",
    "caption",
    "description",
    "sort_order",
    "open_in_new_tab",
    "original_image_url",
    "original_destination_url",
    "original_filename",
    "parse_method",
    "parse_warning",
]
PHOTO_DESCRIPTION_CSV_FIELDS = [
    "gallery_slug",
    "photo_name",
    "photo_slug",
    "source_filename",
    "source_path",
    "full_description",
    "date_or_century",
    "location",
    "material",
    "dimensions",
    "museum_or_collection",
    "institution_or_owner",
    "accession_number",
    "sort_order",
    "parse_method",
    "parse_warning",
    "image_url",
    "destination_url",
    "alt_text",
    "caption",
    "open_in_new_tab",
    "original_image_url",
    "original_destination_url",
    "original_filename",
]


def stable_slug(value: str, fallback: str = "photo") -> str:
    """Generate a stable Webflow-safe slug."""

    value = normalize_text_fragment(value).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-") or fallback


def destination_slug(url: str) -> str:
    """Extract a normalized child post slug from a destination URL."""

    segments = [part for part in urlparse(url).path.split("/") if part]
    return stable_slug(unquote(segments[-1])) if segments else ""


def is_internal_url(url: str, site_host: str = "thekabilincenter.org") -> bool:
    """Return whether a URL belongs to the WordPress site."""

    host = urlparse(url).netloc.casefold().split(":", 1)[0]
    return not host or host == site_host or host.endswith("." + site_host)


def _walk_elementor(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from _walk_elementor(node.get("elements", []))


def _elementor_nodes(item: WordPressItem) -> list[dict[str, Any]]:
    value = item.first_meta("_elementor_data")
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(_walk_elementor(loaded if isinstance(loaded, list) else []))


def extract_child_description(item: WordPressItem) -> tuple[str, str]:
    """Extract the object description from Elementor or rendered post HTML."""

    candidates: list[str] = []
    for node in _elementor_nodes(item):
        if node.get("widgetType") == "text-editor":
            text = html_to_description(
                str(node.get("settings", {}).get("editor", ""))
            )
            if text:
                candidates.append(text)
    if candidates:
        return max(candidates, key=len), "child_elementor_text_editor"
    rendered = html_to_description(item.content)
    rendered = "\n\n".join(
        block
        for block in rendered.split("\n\n")
        if block.casefold() not in {"back to gallery", "previous", "next"}
    )
    if rendered:
        return rendered, "child_content_encoded"
    for key in ("description", "_description", "object_description"):
        value = item.first_meta(key)
        if value:
            return normalize_description(value), f"child_postmeta:{key}"
    return "", "missing"


def extract_child_carousel_image(item: WordPressItem) -> tuple[str, str]:
    """Return the first object-carousel image URL and alt text."""

    for node in _elementor_nodes(item):
        if node.get("widgetType") != "media-carousel":
            continue
        slides = node.get("settings", {}).get("slides", [])
        if slides and isinstance(slides[0], dict):
            image = slides[0].get("image", {})
            return str(image.get("url", "")), str(image.get("alt", ""))
    return "", ""


def _attachment_indexes(
    items: Iterable[WordPressItem],
) -> tuple[dict[str, WordPressItem], dict[int, WordPressItem]]:
    by_key: dict[str, WordPressItem] = {}
    by_id: dict[int, WordPressItem] = {}
    for item in items:
        if item.post_type != "attachment" or not item.attachment_url:
            continue
        by_key[logical_image_key(Path(urlparse(item.attachment_url).path))] = item
        by_id[item.post_id] = item
    return by_key, by_id


def _resolve_local_image(
    images_root: Path, gallery_slug: str, image_url: str
) -> Optional[ImageCandidate]:
    folder = images_root / gallery_slug
    source_path = Path(unquote(urlparse(image_url).path))
    stem, _, _, _ = parse_dimension_suffix(source_path.stem)
    stem = re.sub(r"-(?:scaled|rotated|edited)$", "", stem, flags=re.I)
    source_key = re.sub(
        r"-+", "-", re.sub(r"[\s_]+", "-", stem.casefold())
    ).strip("-")
    keys = {logical_image_key(source_path), source_key}
    candidates: list[ImageCandidate] = []
    if not folder.is_dir():
        return None
    for path in folder.rglob("*"):
        if not path.is_file() or logical_image_key(path) not in keys:
            continue
        candidate = read_image_candidate(path, gallery_slug)
        if candidate is not None:
            candidates.append(candidate)
    return select_highest_resolution_image(candidates) if candidates else None


def extract_gallery_photo_records(
    xml_path: Path,
    images_root: Path,
    *,
    approved_slugs: set[str] = APPROVED_GALLERY_SLUGS,
    special_filename: str = "Special-Features-Picture.png",
) -> list[GalleryPhotoRecord]:
    """Extract ordered photo records from approved gallery parent pages."""

    items = parse_wordpress_items(xml_path)
    posts_by_slug = {
        item.slug: item for item in items if item.slug and item.post_type != "attachment"
    }
    attachments_by_key, _ = _attachment_indexes(items)
    records: list[GalleryPhotoRecord] = []
    used_slugs: set[str] = set()
    for gallery_slug in sorted(approved_slugs):
        parent = posts_by_slug.get(gallery_slug)
        if parent is None:
            LOGGER.error("Approved parent gallery missing from XML: %s", gallery_slug)
            continue
        soup = BeautifulSoup(parent.content, "html.parser")
        sort_order = 0
        for anchor in soup.find_all("a"):
            image = anchor.find("img")
            destination = str(anchor.get("href") or "").strip()
            image_url = str(image.get("src") or "").strip() if image else ""
            if not image or not destination or not image_url:
                continue
            image_path = Path(unquote(urlparse(image_url).path))
            if is_hero_image(image_path) or is_special_features_picture(
                image_path, special_filename
            ):
                continue
            sort_order += 1
            child_slug = destination_slug(destination)
            child = posts_by_slug.get(child_slug)
            warnings: list[str] = []
            if child is None:
                warnings.append("Destination child post was not found in the XML.")
            name = normalize_text_fragment(
                child.title if child else str(image.get("alt") or child_slug)
            )
            description, parse_method = (
                extract_child_description(child)
                if child
                else ("", "missing_child_post")
            )
            if not description:
                warnings.append("No photo description was found.")
            if description and name:
                description = f"{name}\n\n{description}"
            carousel_url, carousel_alt = (
                extract_child_carousel_image(child) if child else ("", "")
            )
            attachment = attachments_by_key.get(logical_image_key(image_path))
            alt_text = normalize_text_fragment(
                str(image.get("alt") or "")
                or carousel_alt
                or (
                    attachment.first_meta("_wp_attachment_image_alt")
                    if attachment
                    else ""
                )
                or name
            )
            caption = html_to_description(attachment.excerpt) if attachment else ""
            local = _resolve_local_image(images_root, gallery_slug, image_url)
            if local is None:
                warnings.append("No local image file matched the parent thumbnail.")
            base_slug = stable_slug(f"{gallery_slug}-{child_slug or name}")
            photo_slug = base_slug
            suffix = 2
            while photo_slug in used_slugs:
                photo_slug = f"{base_slug}-{suffix}"
                suffix += 1
            used_slugs.add(photo_slug)
            original_filename = Path(unquote(urlparse(image_url).path)).name
            records.append(
                GalleryPhotoRecord(
                    name=name or original_filename or f"Photo {sort_order}",
                    slug=photo_slug,
                    gallery_slug=gallery_slug,
                    image_filename=local.path.name if local else "",
                    image_path=str(local.path) if local else "",
                    image_url=carousel_url or image_url,
                    destination_url=destination,
                    alt_text=alt_text,
                    caption=caption,
                    description=description,
                    sort_order=sort_order,
                    open_in_new_tab=(
                        str(anchor.get("target") or "").casefold() == "_blank"
                        or not is_internal_url(destination)
                    ),
                    original_image_url=image_url,
                    original_destination_url=destination,
                    original_filename=original_filename,
                    parse_method=(
                        f"parent_anchor_img+{parse_method}"
                        + ("+attachment_metadata" if attachment else "")
                    ),
                    parse_warning=" ".join(warnings),
                )
            )
    return records


def write_gallery_photo_csv(
    records: Iterable[GalleryPhotoRecord], output_path: Path
) -> None:
    """Write photo records with correct multiline CSV quoting."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PHOTO_CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    field: (
                        str(getattr(record, field)).lower()
                        if field == "open_in_new_tab"
                        else getattr(record, field)
                    )
                    for field in PHOTO_CSV_FIELDS
                }
            )


def write_gallery_photo_description_csv(
    records: Iterable[GalleryPhotoRecord], output_path: Path
) -> list[GalleryPhotoRecord]:
    """Write the additive structured photo-description CSV."""

    enriched = [enrich_photo_record(record) for record in records]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=PHOTO_DESCRIPTION_CSV_FIELDS
        )
        writer.writeheader()
        for record in enriched:
            writer.writerow(
                {
                    "gallery_slug": record.gallery_slug,
                    "photo_name": record.name,
                    "photo_slug": record.slug,
                    "source_filename": record.image_filename,
                    "source_path": record.image_path,
                    "full_description": record.description,
                    "date_or_century": record.date_or_century,
                    "location": record.location,
                    "material": record.material,
                    "dimensions": record.dimensions,
                    "museum_or_collection": record.museum_or_collection,
                    "institution_or_owner": record.institution_or_owner,
                    "accession_number": record.accession_number,
                    "sort_order": record.sort_order,
                    "parse_method": record.parse_method,
                    "parse_warning": record.parse_warning,
                    "image_url": record.image_url,
                    "destination_url": record.destination_url,
                    "alt_text": record.alt_text,
                    "caption": record.caption,
                    "open_in_new_tab": str(record.open_in_new_tab).lower(),
                    "original_image_url": record.original_image_url,
                    "original_destination_url": (
                        record.original_destination_url
                    ),
                    "original_filename": record.original_filename,
                }
            )
    return enriched
