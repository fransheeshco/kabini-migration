from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from migration.description_parser import (
    description_to_rich_text,
    html_to_description,
    normalize_description,
)
from migration.gallery_photo_extractor import (
    destination_slug,
    extract_child_description,
    extract_gallery_photo_records,
    is_internal_url,
    stable_slug,
    write_gallery_photo_csv,
)
from migration.gallery_photo_migrator import (
    build_gallery_photo_payload,
    load_gallery_photo_rows,
    pending_gallery_photo_records,
    validate_child_schema,
    validate_gallery_photo_rows,
)
from migration.models import GalleryPhotoRecord
from migration.xml_parser import WordPressItem


def photo_record(root: Path) -> GalleryPhotoRecord:
    image = root / "gallery" / "photo.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2000, 1500), "red").save(image)
    return GalleryPhotoRecord(
        name="Santo Niño",
        slug="gallery-santo-nino",
        gallery_slug="gallery",
        image_filename=image.name,
        image_path=str(image),
        image_url="https://example.org/photo.jpg",
        destination_url="https://example.org/santo-nino/",
        alt_text="Santo Niño",
        caption="Museum object",
        description="Santo Niño\n\n18th century\n\nCebu",
        sort_order=1,
        open_in_new_tab=True,
        original_image_url="https://example.org/photo.jpg",
        original_destination_url="https://example.org/santo-nino/",
        original_filename="photo.jpg",
        parse_method="fixture",
        parse_warning="",
        source_row=2,
    )


class GalleryPhotoTests(unittest.TestCase):
    def test_multiline_description_normalization(self) -> None:
        html = (
            "<h3>SANTO NIÑO\u200b</h3>"
            "<p>18th&nbsp;century</p><p>Barili, Cebu</p>"
            "<p>Accession number: ABC-001</p>"
        )
        self.assertEqual(
            html_to_description(html),
            "SANTO NIÑO\n\n18th century\n\nBarili, Cebu\n\n"
            "Accession number: ABC-001",
        )
        self.assertEqual(
            normalize_description("One\xa0 line\u200b\n\n\nTwo"),
            "One line\n\nTwo",
        )

    def test_child_elementor_description_and_title_association(self) -> None:
        item = WordPressItem(
            1,
            "post",
            "publish",
            "SANTO NIÑO",
            "santo-nino",
            "https://example.org/santo-nino/",
            "",
            "",
            "",
            {
                "_elementor_data": [
                    '[{"widgetType":"text-editor","settings":{"editor":'
                    '"<p>18th century</p><p>Cebu</p>"},"elements":[]}]'
                ]
            },
        )
        description, method = extract_child_description(item)
        self.assertEqual(description, "18th century\n\nCebu")
        self.assertEqual(method, "child_elementor_text_editor")

    def test_urls_and_stable_slugs(self) -> None:
        self.assertEqual(
            destination_slug("https://thekabilincenter.org/Santo-Nino/"),
            "santo-nino",
        )
        self.assertEqual(stable_slug("Santo Niño"), "santo-ni-o")
        self.assertTrue(
            is_internal_url("https://thekabilincenter.org/santo-nino/")
        )
        self.assertFalse(is_internal_url("https://museum.example/object"))

    def test_multiline_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = photo_record(root)
            output = root / "photos.csv"
            write_gallery_photo_csv([record], output)
            loaded = load_gallery_photo_rows(output)
            self.assertEqual(loaded[0].description, record.description)

    def test_child_schema_and_payload(self) -> None:
        schema = {
            "fields": [
                {"displayName": "Name", "slug": "name", "type": "PlainText"},
                {"displayName": "Slug", "slug": "slug", "type": "PlainText"},
                {"displayName": "Photo", "slug": "photo", "type": "Image"},
                {
                    "displayName": "TOD Gallery",
                    "slug": "tod-gallery",
                    "type": "Reference",
                    "validations": {"collectionId": "parent"},
                },
                {
                    "displayName": "Sort Order",
                    "slug": "sort-order",
                    "type": "Number",
                },
                {
                    "displayName": "Description",
                    "slug": "description",
                    "type": "RichText",
                },
            ]
        }
        self.assertEqual(validate_child_schema(schema, "parent"), [])
        with tempfile.TemporaryDirectory() as directory:
            record = photo_record(Path(directory))
            payload = build_gallery_photo_payload(
                record,
                schema,
                "parent-item",
                {"fileId": "asset", "url": "https://cdn/photo.jpg"},
            )
        fields = payload["fieldData"]
        self.assertEqual(fields["tod-gallery"], "parent-item")
        self.assertEqual(fields["sort-order"], 1)
        self.assertIn("<strong>Santo Niño</strong>", fields["description"])

    def test_validation_missing_optional_values_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = photo_record(Path(directory))
            record = GalleryPhotoRecord(
                **{
                    **record.__dict__,
                    "description": "",
                    "destination_url": "",
                }
            )
            errors, warnings = validate_gallery_photo_rows(
                [record], {"gallery"}
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)

    def test_stale_completed_photo_is_retried_when_missing_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = photo_record(Path(directory))
            completed = {
                record.slug: {
                    "status": "created",
                    "webflowItemId": "deleted-item",
                }
            }
            self.assertEqual(
                pending_gallery_photo_records(
                    [record], completed, set(), dry_run=False
                ),
                [record],
            )
            self.assertEqual(
                pending_gallery_photo_records(
                    [record], completed, {record.slug}, dry_run=False
                ),
                [],
            )
