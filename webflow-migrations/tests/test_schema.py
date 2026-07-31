import unittest

from migration.webflow_client import (
    find_special_features_picture_field,
)


class SchemaTests(unittest.TestCase):
    def test_special_field_uses_detected_api_slug(self) -> None:
        field = find_special_features_picture_field(
            [
                {
                    "displayName": "Special Features Picture",
                    "slug": "actual-special-slug",
                    "type": "Image",
                }
            ]
        )
        self.assertEqual(field["slug"], "actual-special-slug")
