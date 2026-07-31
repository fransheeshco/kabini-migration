#!/usr/bin/env python3
"""Generate the structured TOD Gallery Photos description CSV."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from migration.gallery_photo_extractor import (
    APPROVED_GALLERY_SLUGS,
    extract_gallery_photo_records,
    write_gallery_photo_description_csv,
)
from migration.images import (
    classify_gallery_images,
    discover_image_candidates,
    logical_image_key,
)


def parse_args() -> argparse.Namespace:
    """Parse description-extraction CLI arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "xml",
        nargs="?",
        default="../thekabilincenter.WordPress.2026-07-21.xml",
    )
    parser.add_argument("--images-root", default="../tod-gallery-images")
    parser.add_argument(
        "--output", default="tod-gallery-photo-descriptions.csv"
    )
    return parser.parse_args()


def main() -> int:
    """Extract XML-linked photos, parse descriptions, and write the CSV."""

    args = parse_args()
    records = extract_gallery_photo_records(
        Path(args.xml), Path(args.images_root)
    )
    enriched = write_gallery_photo_description_csv(
        records, Path(args.output)
    )
    statuses = Counter()
    for record in enriched:
        if not record.description:
            statuses["missing"] += 1
        elif record.parse_warning:
            statuses["partial"] += 1
        else:
            statuses["full"] += 1
    accession_counts = Counter(
        accession
        for record in enriched
        for accession in record.accession_number.split("\n\n")
        if accession
    )
    duplicate_accessions = sum(
        count > 1 for count in accession_counts.values()
    )
    matched_keys = {
        (
            record.gallery_slug,
            logical_image_key(Path(record.image_path)),
        )
        for record in enriched
        if record.image_path
    }
    unmatched_source_images = 0
    for gallery_slug in sorted(APPROVED_GALLERY_SLUGS):
        classified = classify_gallery_images(
            discover_image_candidates(Path(args.images_root), gallery_slug)
        )
        unmatched_source_images += sum(
            (gallery_slug, image.logical_key) not in matched_keys
            for image in classified.gallery_images
        )
    unmatched_xml = sum(
        "Destination child post was not found" in record.parse_warning
        for record in enriched
    )
    print(f"Gallery photos: {len(enriched):,}")
    print(f"Fully parsed: {statuses['full']:,}")
    print(f"Partially parsed: {statuses['partial']:,}")
    print(f"Missing descriptions: {statuses['missing']:,}")
    print(f"Unmatched XML child records: {unmatched_xml:,}")
    print(f"Unmatched source image families: {unmatched_source_images:,}")
    print(f"Duplicate accession values: {duplicate_accessions:,}")
    print(f"Output: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
