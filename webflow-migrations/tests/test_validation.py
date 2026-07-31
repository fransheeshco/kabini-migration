import tempfile
import unittest
from pathlib import Path

from migration.exceptions import ValidationError
from migration.models import ProcessedImage
from migration.validation import validate_payload_image_paths


class ValidationTests(unittest.TestCase):
    def test_missing_processed_photo_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.jpg"
            image = ProcessedImage(
                missing,
                missing,
                "gallery",
                "photo",
                1600,
                1200,
                1,
            )
            with self.assertRaisesRegex(ValidationError, "does not exist"):
                validate_payload_image_paths({"photo": image})
