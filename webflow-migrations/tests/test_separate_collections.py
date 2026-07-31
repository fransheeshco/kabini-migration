from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from migration.exceptions import MigrationError
from migration.gallery_photo_migrator import validate_gallery_photo_rows
from migration.images import classify_gallery_images, discover_image_candidates
from migration.models import GalleryPhotoRecord
from migration.runner import build_webflow_item


def photo_record(path: Path, gallery_slug: str, slug: str) -> GalleryPhotoRecord:
    """Build a minimal owned photo record for architecture tests."""

    return GalleryPhotoRecord(
        name="Repeated title",
        slug=slug,
        gallery_slug=gallery_slug,
        image_filename=path.name,
        image_path=str(path),
        image_url="https://example.org/photo.jpg",
        destination_url="https://example.org/object/",
        alt_text="Repeated title",
        caption="",
        description="Repeated title\n\n18th century",
        sort_order=1,
        open_in_new_tab=False,
        original_image_url="https://example.org/photo.jpg",
        original_destination_url="https://example.org/object/",
        original_filename=path.name,
        parse_method="test",
        parse_warning="",
        source_row=2,
    )


class SeparateCollectionsTests(unittest.TestCase):
    def test_identical_filenames_remain_owned_by_separate_galleries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for slug, color in (("gallery-a", "red"), ("gallery-b", "blue")):
                folder = root / slug
                folder.mkdir()
                Image.new("RGB", (2000, 1500), color).save(folder / "photo.jpg")

            gallery_a = classify_gallery_images(
                discover_image_candidates(root, "gallery-a")
            )
            gallery_b = classify_gallery_images(
                discover_image_candidates(root, "gallery-b")
            )

            self.assertEqual(len(gallery_a.gallery_images), 1)
            self.assertEqual(len(gallery_b.gallery_images), 1)
            self.assertEqual(gallery_a.gallery_images[0].gallery_slug, "gallery-a")
            self.assertEqual(gallery_b.gallery_images[0].gallery_slug, "gallery-b")
            self.assertNotEqual(
                gallery_a.gallery_images[0].path,
                gallery_b.gallery_images[0].path,
            )

    def test_cross_gallery_source_path_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "gallery-b" / "photo.jpg"
            path.parent.mkdir()
            Image.new("RGB", (2000, 1500), "blue").save(path)
            record = photo_record(path, "gallery-a", "gallery-a-photo")

            errors, _ = validate_gallery_photo_rows(
                [record], {"gallery-a", "gallery-b"}
            )

            self.assertTrue(
                any("different gallery" in error for error in errors),
                errors,
            )

    def test_parent_payload_rejects_legacy_gallery_images_fields(self) -> None:
        schema = {
            "fields": [
                {"displayName": "Name", "slug": "name", "type": "PlainText"},
                {"displayName": "Slug", "slug": "slug", "type": "PlainText"},
                {
                    "displayName": "Gallery Images 2",
                    "slug": "gallery-images-2",
                    "type": "MultiImage",
                },
            ]
        }
        field_map = {"fields": {"title": "name", "slug": "slug"}}
        row = {"title": "Gallery", "slug": "gallery", "_source_row": 2}

        with self.assertRaisesRegex(
            MigrationError, "cannot be included in a TOD Gallery payload"
        ):
            build_webflow_item(
                row,
                field_map,
                schema,
                {},
                {"gallery-images-2": [{"fileId": "legacy"}]},
            )

    def test_parent_payload_accepts_only_gallery_level_images(self) -> None:
        schema = {
            "fields": [
                {"displayName": "Name", "slug": "name", "type": "PlainText"},
                {"displayName": "Slug", "slug": "slug", "type": "PlainText"},
                {"displayName": "Hero Image", "slug": "hero-image", "type": "Image"},
                {
                    "displayName": "Special Features Picture",
                    "slug": "special-features-picture",
                    "type": "Image",
                },
            ]
        }
        item = build_webflow_item(
            {"title": "Gallery", "slug": "gallery", "_source_row": 2},
            {"fields": {"title": "name", "slug": "slug"}},
            schema,
            {},
            {
                "hero-image": {"fileId": "hero"},
                "special-features-picture": {"fileId": "special"},
            },
        )
        self.assertEqual(
            set(item["fieldData"]),
            {"name", "slug", "hero-image", "special-features-picture"},
        )


if __name__ == "__main__":
    unittest.main()
