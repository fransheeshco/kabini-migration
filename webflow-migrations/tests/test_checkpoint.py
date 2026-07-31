from pathlib import Path
import tempfile
import unittest

from migration.checkpoint import load_checkpoint, save_json_atomic
from migration.exceptions import MigrationError


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_serialization_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            path = tmp_path / "checkpoint.json"
            checkpoint = load_checkpoint(path)
            checkpoint["completed"]["gallery"] = {"status": "created"}
            checkpoint["processed_images"]["key"] = {"path": "image.jpg"}
            save_json_atomic(path, checkpoint)
            resumed = load_checkpoint(path)
            self.assertEqual(
                resumed["completed"]["gallery"]["status"], "created"
            )
            self.assertEqual(
                resumed["processed_images"]["key"]["path"], "image.jpg"
            )
            self.assertFalse(list(tmp_path.glob("*.tmp")))

    def test_corrupted_checkpoint_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "Invalid checkpoint"):
                load_checkpoint(path)
