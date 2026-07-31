import tempfile
import unittest
from pathlib import Path

from migration.models import ProcessedImage
from migration.payloads import (
    omit_empty_optional_fields,
    valid_image_assets,
)


class PayloadTests(unittest.TestCase):
    def test_payload_omits_skipped_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "exists.jpg"
            existing.write_bytes(b"x")
            good = ProcessedImage(
                existing, existing, "slug", "one", 1600, 1200, 1
            )
            missing_path = root / "missing.jpg"
            missing = ProcessedImage(
                missing_path, missing_path, "slug", "two", 1600, 1200, 1
            )
            self.assertEqual(valid_image_assets([good, missing]), [good])
            self.assertEqual(
                omit_empty_optional_fields(
                    {
                        "hero": None,
                        "gallery": [],
                        "name": "Gallery",
                        "draft": False,
                    }
                ),
                {"name": "Gallery", "draft": False},
            )
