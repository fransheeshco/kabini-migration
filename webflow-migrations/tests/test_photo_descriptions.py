from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from migration.gallery_photo_extractor import (
    PHOTO_DESCRIPTION_CSV_FIELDS,
    write_gallery_photo_description_csv,
)
from migration.gallery_photo_migrator import (
    build_gallery_photo_payload,
    load_gallery_photo_rows,
)
from migration.models import GalleryPhotoRecord
from migration.photo_description_parser import parse_photo_description


class PhotoDescriptionTests(unittest.TestCase):
    def test_seven_structured_fields_are_parsed(self) -> None:
        parsed = parse_photo_description(
            "SANTO NIÑO\n\n19th century\n\n"
            "Paknaan, Mandaue City, Cebu\n\n"
            "De tallado, polychromed wood\n\n"
            "Height with the base: 12.7cm\n\n"
            "STC Folklife Museum\n\n"
            "Saint Theresa’s College, Cebu City\n\n"
            "Accession number: STC-205",
            "SANTO NIÑO",
        )
        self.assertEqual(parsed.date_or_century, "19th century")
        self.assertEqual(parsed.location, "Paknaan, Mandaue City, Cebu")
        self.assertEqual(parsed.material, "De tallado, polychromed wood")
        self.assertEqual(parsed.dimensions, "Height with the base: 12.7cm")
        self.assertEqual(parsed.museum_or_collection, "STC Folklife Museum")
        self.assertEqual(
            parsed.institution_or_owner,
            "Saint Theresa’s College, Cebu City",
        )
        self.assertEqual(parsed.accession_number, "STC-205")
        self.assertEqual(parsed.parse_warning, "")

    def test_missing_fields_do_not_shift_later_values(self) -> None:
        parsed = parse_photo_description(
            "OBJECT\n\nPainted wood\n\nHeight: 20cm\n\n"
            "Acc. No.: ABC-7",
            "OBJECT",
        )
        self.assertEqual(parsed.date_or_century, "")
        self.assertEqual(parsed.location, "")
        self.assertEqual(parsed.material, "Painted wood")
        self.assertEqual(parsed.dimensions, "Height: 20cm")
        self.assertEqual(parsed.accession_number, "ABC-7")

    def test_unresolved_lines_are_preserved_and_warned(self) -> None:
        source = "OBJECT\n\nUnclassified historical note"
        parsed = parse_photo_description(source, "OBJECT")
        self.assertEqual(parsed.full_description, source)
        self.assertIn("Unresolved description block", parsed.parse_warning)

    def test_description_csv_round_trip_and_schema_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "gallery" / "photo.jpg"
            image.parent.mkdir()
            Image.new("RGB", (1600, 1200), "red").save(image)
            record = GalleryPhotoRecord(
                "Santo Niño",
                "gallery-santo-nino",
                "gallery",
                image.name,
                str(image),
                "",
                "",
                "",
                "",
                "Santo Niño\n\n19th century\n\nCebu\n\nPainted wood\n\n"
                "Height: 20cm\n\nMuseum\n\nCollege\n\n"
                "Accession no.: STC-1",
                1,
                False,
                "",
                "",
                image.name,
                "fixture",
                "",
            )
            output = root / "descriptions.csv"
            write_gallery_photo_description_csv([record], output)
            loaded = load_gallery_photo_rows(output)[0]
            with output.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(
                    csv.DictReader(handle).fieldnames,
                    PHOTO_DESCRIPTION_CSV_FIELDS,
                )
            self.assertEqual(loaded.accession_number, "STC-1")

            schema = {
                "fields": [
                    {"displayName": "Photo Name", "slug": "name-x", "type": "PlainText"},
                    {"displayName": "Slug", "slug": "slug-x", "type": "PlainText"},
                    {"displayName": "Image", "slug": "image-x", "type": "Image"},
                    {"displayName": "Gallery", "slug": "gallery-x", "type": "Reference"},
                    {"displayName": "Sort Order", "slug": "order-x", "type": "Number"},
                    {"displayName": "Accession Number", "slug": "accession-x", "type": "PlainText"},
                    {"displayName": "Material", "slug": "material-x", "type": "PlainText"},
                ]
            }
            payload = build_gallery_photo_payload(
                loaded,
                schema,
                "parent-id",
                {"fileId": "asset"},
            )["fieldData"]
            self.assertEqual(payload["gallery-x"], "parent-id")
            self.assertEqual(payload["accession-x"], "STC-1")
            self.assertEqual(payload["material-x"], "Painted wood")


if __name__ == "__main__":
    unittest.main()
