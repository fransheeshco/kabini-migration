from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from migration.images import (
    build_processed_image_path,
    classify_gallery_images,
    find_special_features_picture,
    get_image_dimensions,
    is_hero_image,
    is_special_features_picture,
    process_single_image,
    select_highest_resolution_image,
)
from migration.models import ImageCandidate


def candidate(
    path: Path,
    width: int,
    height: int,
    size: int,
    key: str = "photo",
) -> ImageCandidate:
    return ImageCandidate(path, "gallery", key, width, height, size)


class ImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_highest_resolution_duplicate_selection(self) -> None:
        low = candidate(self.root / "photo-800x600.jpg", 800, 600, 500)
        high = candidate(
            self.root / "photo-1600x1200.jpg", 1600, 1200, 400
        )
        self.assertEqual(select_highest_resolution_image([low, high]), high)

    def test_duplicate_tie_breaking_is_deterministic(self) -> None:
        narrow = candidate(self.root / "z.jpg", 1200, 1600, 900)
        wide_small = candidate(self.root / "b.jpg", 1600, 1200, 800)
        wide_large_z = candidate(self.root / "z2.jpg", 1600, 1200, 1000)
        wide_large_a = candidate(self.root / "a.jpg", 1600, 1200, 1000)
        self.assertEqual(
            select_highest_resolution_image(
                [narrow, wide_small, wide_large_z, wide_large_a]
            ),
            wide_large_a,
        )

    def test_exif_orientation_is_applied(self) -> None:
        path = self.root / "oriented.jpg"
        image = Image.new("RGB", (40, 80), "red")
        exif = image.getexif()
        exif[274] = 6
        image.save(path, exif=exif)
        self.assertEqual(get_image_dimensions(path), (80, 40))

    def test_center_crop_has_exact_dimensions_and_ratio(self) -> None:
        source = self.root / "wide.jpg"
        Image.new("RGB", (2400, 1200), "blue").save(source)
        output = self.root / "processed" / "image.jpg"
        result = process_single_image(
            candidate(source, 2400, 1200, source.stat().st_size),
            output,
            target_width=1600,
            target_height=1200,
        )
        self.assertIsNotNone(result)
        self.assertEqual(get_image_dimensions(output), (1600, 1200))
        assert result is not None
        self.assertEqual(result.width / result.height, 4 / 3)

    def test_low_resolution_is_skipped(self) -> None:
        source = self.root / "small.jpg"
        Image.new("RGB", (800, 600), "blue").save(source)
        output = self.root / "out.jpg"
        result = process_single_image(
            candidate(source, 800, 600, source.stat().st_size),
            output,
            allow_upscaling=False,
        )
        self.assertIsNone(result)
        self.assertFalse(output.exists())

    def test_transparent_png_preserves_alpha(self) -> None:
        source = self.root / "transparent.png"
        Image.new("RGBA", (2000, 1400), (0, 0, 0, 0)).save(source)
        output = self.root / "processed.png"
        result = process_single_image(
            candidate(source, 2000, 1400, source.stat().st_size), output
        )
        self.assertIsNotNone(result)
        with Image.open(output) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.size, (1600, 1200))

    def test_processed_filename_is_deterministic(self) -> None:
        source = self.root / "photo.jpg"
        source.write_bytes(b"source")
        first = build_processed_image_path(
            self.root / "out", "gallery", 1, source
        )
        second = build_processed_image_path(
            self.root / "out", "gallery", 1, source
        )
        self.assertEqual(first, second)
        self.assertEqual(first.parent.name, "gallery")

    def test_reserved_image_detection(self) -> None:
        self.assertTrue(is_hero_image(Path("099-G3-Icon.png")))
        self.assertFalse(is_hero_image(Path("ordinary-photo.png")))
        self.assertTrue(
            is_special_features_picture(
                Path("003-Special-Features-Picture-1600x1200.png"),
                "Special-Features-Picture.png",
            )
        )

    def test_special_features_filename_variations(self) -> None:
        for filename in (
            "Special-Features-Picture.png",
            "special-features-picture.PNG",
            "Special Features Picture.png",
            "special_features_picture.png",
            "special-features-picture.jpg",
        ):
            with self.subTest(filename=filename):
                self.assertTrue(is_special_features_picture(Path(filename)))

    def test_multiple_special_candidates_choose_highest_resolution(self) -> None:
        low = candidate(
            self.root / "Special Features Picture.png",
            800,
            600,
            100,
            "special-low",
        )
        high = candidate(
            self.root / "special_features_picture.jpg",
            1600,
            1200,
            200,
            "special-high",
        )
        self.assertEqual(
            find_special_features_picture([low, high]),
            high,
        )

    def test_classification_excludes_hero_and_special(self) -> None:
        hero = ImageCandidate(
            self.root / "099-G1-Icon.jpg",
            "gallery",
            "g1-icon",
            2000,
            1500,
            200,
            is_hero=True,
        )
        special = candidate(
            self.root / "Special Features Picture.png",
            2000,
            1500,
            200,
            "special-features-picture",
        )
        gallery = [
            candidate(
                self.root / f"{index:03d}-photo-{index}.jpg",
                2000,
                1500,
                200,
                f"photo-{index}",
            )
            for index in range(46)
        ]
        classified = classify_gallery_images([hero, special, *gallery])
        self.assertEqual(classified.hero_image, hero)
        self.assertEqual(classified.special_features_picture, special)
        self.assertEqual(len(classified.gallery_images), 46)
        self.assertNotIn(hero, classified.gallery_images)
        self.assertNotIn(special, classified.gallery_images)

    def test_missing_special_picture_is_optional(self) -> None:
        self.assertIsNone(find_special_features_picture([]))
