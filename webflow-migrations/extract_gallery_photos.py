#!/usr/bin/env python3
"""Generate the TOD gallery-photo CSV from WordPress XML."""

from __future__ import annotations

import argparse
from pathlib import Path

from migration.gallery_photo_extractor import (
    extract_gallery_photo_records,
    write_gallery_photo_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "xml",
        nargs="?",
        default="../thekabilincenter.WordPress.2026-07-21.xml",
    )
    parser.add_argument("--images-root", default="../tod-gallery-images")
    parser.add_argument("--output", default="tod_gallery_photos.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = extract_gallery_photo_records(
        Path(args.xml), Path(args.images_root)
    )
    write_gallery_photo_csv(records, Path(args.output))
    descriptions = sum(bool(record.description) for record in records)
    destinations = sum(bool(record.destination_url) for record in records)
    unresolved = sum(bool(record.parse_warning) for record in records)
    print(f"TOD gallery photos: {len(records):,}")
    print(f"Descriptions found: {descriptions:,}")
    print(f"Descriptions missing: {len(records) - descriptions:,}")
    print(f"Destination URLs found: {destinations:,}")
    print(f"Destination URLs missing: {len(records) - destinations:,}")
    print(f"Records with warnings: {unresolved:,}")
    print(f"Output: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
