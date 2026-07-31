from __future__ import annotations

import tempfile
import unittest
import csv
from pathlib import Path
from unittest.mock import Mock

from migration.exceptions import MigrationError
from migration.photo_sets import (
    derive_photo_set_identity,
    clean_generated_filename,
    deterministic_set_slug,
    ensure_reference_field,
    resolve_parent_reference_field,
    run_photo_sets,
    update_env_value_safely,
    validate_photo_sets_schema,
)
from migration.webflow_client import WebflowClient


class FakeClient:
    def __init__(self, *, existing_set: bool = False, incompatible: bool = False):
        self.site_id = "site"
        self.collection_id = "parents"
        self.posts = []
        self.patches = []
        self.field_posts = []
        self.collections = [
            {"id": "parents", "displayName": "TOD Galleries", "slug": "galleries"},
            {"id": "photos", "displayName": "TOD Gallery Photos", "slug": "photos"},
            {"id": "sets", "displayName": "TOD Photo Sets", "slug": "tod-photo-sets"},
        ]
        field = {"id": "ref", "slug": "photo-set-reference", "displayName": "Photo Set Reference", "type": "PlainText" if incompatible else "Reference", "metadata": {"collectionId": "sets"}}
        self.schemas = {
            "photos": {"id": "photos", "fields": [
                {"id": "parent-ref", "slug": "tod-gallery", "displayName": "TOD Gallery", "type": "Reference", "validations": {"collectionId": "parents"}},
                field,
            ]},
            "sets": {"id": "sets", "fields": [
                {"slug": "gallery-reference", "type": "Reference", "metadata": {"collectionId": "parents"}},
                {"slug": "description", "type": "PlainText"},
                {"slug": "sort-order", "type": "Number"},
                {"slug": "cover-image", "type": "Image"},
            ]},
        }
        self.items = {
            "parents": [
                {"id": "g1", "fieldData": {"slug": "gallery-a"}},
                {"id": "g2", "fieldData": {"slug": "gallery-b"}},
            ],
            "photos": [
                {"id": "p1", "isDraft": False, "fieldData": {"name": "SANTO NINO", "original-filename": "santo-nino-1.jpg", "tod-gallery": "g1", "sort-order": 2, "image": {"url": "one.jpg"}, "unrelated": "untouched"}},
                {"id": "p2", "fieldData": {"name": "Santo Nino", "original-filename": "santo-nino-2.jpg", "tod-gallery": {"id": "g1"}, "sort-order": 3}},
                {"id": "p3", "fieldData": {"name": "SANTO NINO", "original-filename": "santo-nino-1.jpg", "tod-gallery": "g2"}},
                {"id": "missing", "fieldData": {"original-filename": "orphan-1.jpg"}},
            ],
            "sets": ([{"id": "s1", "fieldData": {"slug": "gallery-a-santo-nino", "gallery-reference": "g1"}}] if existing_set else []),
        }

    def list_collections(self): return self.collections
    def get_collection_schema(self, collection_id=None): return self.schemas[collection_id]
    def list_items(self, collection_id=None, page_limit=100): return list(self.items[collection_id or self.collection_id])
    def create_collection_field(self, collection_id, payload): self.field_posts.append(payload); return {"id": "new-ref"}
    def create_collection(self, payload): self.posts.append(("collection", payload)); return {"id": "sets"}
    def create_items(self, items):
        self.posts.append((self.collection_id, items))
        return {"id": f"new-{len(self.posts)}"}
    def update_item(self, item_id, item): self.patches.append((item_id, item)); return {"id": item_id}


class PhotoSetTests(unittest.TestCase):
    def test_resolves_nonstandard_parent_reference_slug_and_validations_target(self):
        field = resolve_parent_reference_field(FakeClient().schemas["photos"], "parents")
        self.assertEqual(field["slug"], "tod-gallery")

    def test_reference_values_string_object_and_empty(self):
        from migration.photo_sets import _reference_value
        self.assertEqual(_reference_value(" item-id "), "item-id")
        self.assertEqual(_reference_value({"id": "object-id"}), "object-id")
        self.assertEqual(_reference_value(None), "")

    def test_exactly_one_parent_reference_match(self):
        schema = {"fields": [
            {"type": "Reference", "slug": "parent", "metadata": {"collectionId": "parents"}},
            {"type": "Reference", "slug": "other", "metadata": {"collectionId": "other"}},
        ]}
        self.assertEqual(resolve_parent_reference_field(schema, "parents")["slug"], "parent")

    def test_multiple_parent_reference_matches_fail(self):
        schema = {"fields": [
            {"type": "Reference", "slug": "parent-a", "metadata": {"collectionId": "parents"}},
            {"type": "Reference", "slug": "parent-b", "validations": {"collectionId": "parents"}},
        ]}
        with self.assertRaisesRegex(MigrationError, "multiple Reference fields"):
            resolve_parent_reference_field(schema, "parents")

    def test_real_shaped_v2_reference_metadata_target(self):
        parent_id = " 6a4128a6e922541887044952 "
        schema = {"id": "sets", "fields": [
            {
                "id": "reference-field",
                "type": "Reference",
                "slug": "gallery-reference",
                "metadata": {"collectionId": "6a4128a6e922541887044952"},
            },
            {"slug": "description", "type": "PlainText"},
            {"slug": "sort-order", "type": "Number"},
            {"slug": "cover-image", "type": "Image"},
        ]}
        validate_photo_sets_schema(schema, parent_id)

    def test_live_validations_shape_is_diagnosed_and_supported(self):
        client = FakeClient()
        field = client.schemas["sets"]["fields"][0]
        field.pop("metadata")
        field["validations"] = {"collectionId": "parents"}
        with self.assertLogs(level="WARNING") as logs:
            validate_photo_sets_schema(client.schemas["sets"], "parents")
        self.assertIn("metadata.collectionId is missing", "\n".join(logs.output))

    def test_genuine_reference_target_mismatch_still_fails_closed(self):
        client = FakeClient()
        client.schemas["sets"]["fields"][0]["metadata"]["collectionId"] = "different"
        with self.assertRaisesRegex(MigrationError, "actual=different, expected=parents"):
            validate_photo_sets_schema(client.schemas["sets"], "parents")

    def test_sequence_removal_and_internal_numbers(self):
        self.assertEqual(derive_photo_set_identity("Santo-Nino-1.jpg").base_slug, "santo-nino")
        self.assertEqual(derive_photo_set_identity("Santo_Nino_01.png").base_slug, "santo-nino")
        self.assertEqual(derive_photo_set_identity("Santo Nino 2.webp").base_slug, "santo-nino")
        self.assertEqual(derive_photo_set_identity("Pope John 23 Portrait-1.jpg").base_slug, "pope-john-23-portrait")

    def test_generated_prefix_cleanup(self):
        one = "6a67a0dc8acb3b3a14198f43_photo-015-chasuble-2.jpeg"
        many = "6a67a0dc8acb3b3a14198f43_6a67a0d9d42dd196cb0a3ca2_photo-015-chasuble-2.jpeg"
        self.assertEqual(clean_generated_filename(one), "chasuble-2")
        self.assertEqual(clean_generated_filename(many), "chasuble-2")

    def test_unproven_meaningful_suffix_is_preserved(self):
        identity = derive_photo_set_identity(
            "pope-john-paul-2.jpg", sequence_suffix_proven=False
        )
        self.assertEqual(identity.base_slug, "pope-john-paul-2")
        self.assertEqual(
            derive_photo_set_identity("st-joseph-19th-century.jpg").base_slug,
            "st-joseph-19th-century",
        )

    def test_clean_human_names_and_slugs(self):
        from migration.photo_sets import humanize_photo_set_name, usable_photo_name
        self.assertEqual(humanize_photo_set_name("DALMATIC (PAIR)"), "Dalmatic (Pair)")
        self.assertEqual(humanize_photo_set_name("CHASUBLE WITH STOLE"), "Chasuble with Stole")
        self.assertEqual(humanize_photo_set_name("NUESTRA SEÑORA"), "Nuestra Señora")
        self.assertEqual(derive_photo_set_identity("dalmatic-pair.jpg").base_slug, "dalmatic-pair")
        self.assertFalse(usable_photo_name("Photo 015"))

    def test_singleton_safety_threshold_blocks_real_writes(self):
        client = FakeClient()
        client.items["photos"] = [
            {"id": f"p{index}", "fieldData": {
                "name": f"Unique Artifact {index}",
                "tod-gallery": "g1",
                "photo": {"url": f"artifact-{index}.jpg"},
            }}
            for index in range(5)
        ]
        with self.assertRaisesRegex(MigrationError, "grouping appears ineffective"):
            run_photo_sets(
                client,
                parent_collection_id="parents",
                photos_collection_id="photos",
                configured_photo_sets_id="sets",
                dry_run=False,
            )
        self.assertEqual(client.posts, [])
        self.assertEqual(client.patches, [])

    def test_deterministic_cross_gallery_slugs(self):
        self.assertEqual(deterministic_set_slug("santo-nino", "gallery-a", "g1"), "santo-nino")
        self.assertNotEqual(
            deterministic_set_slug("santo-nino", "gallery-a", "g1", disambiguate=True),
            deterministic_set_slug("santo-nino", "gallery-b", "g2", disambiguate=True),
        )

    def test_existing_reference_reused_and_incompatible_fails(self):
        client = FakeClient()
        self.assertEqual(ensure_reference_field(client, "photos", "sets", False), ("ref", False))
        with self.assertRaisesRegex(MigrationError, "incompatible"):
            ensure_reference_field(FakeClient(incompatible=True), "photos", "sets", False)

    def test_rerun_reuses_set_and_patch_is_minimal(self):
        client = FakeClient(existing_set=True)
        summary = run_photo_sets(client, parent_collection_id="parents", photos_collection_id="photos", configured_photo_sets_id="sets")
        self.assertEqual(summary["unique_sets"], 2)
        self.assertEqual(summary["sets_reused"], 1)
        self.assertEqual(summary["missing_parent_count"], 1)
        self.assertEqual(summary["missing_parent"][0]["photo_item_id"], "missing")
        self.assertEqual(summary["missing_parent"][0]["reason"], "reference field absent")
        self.assertTrue(all(set(payload["fieldData"]) == {"photo-set-reference"} for _, payload in client.patches))
        self.assertEqual(client.items["photos"][0]["fieldData"]["unrelated"], "untouched")

    def test_dry_run_performs_no_mutations_or_env_write(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            review = Path(directory) / "review.csv"
            singletons = Path(directory) / "singletons.csv"
            env.write_text("KEEP=yes\n", encoding="utf-8")
            summary = run_photo_sets(
                client, parent_collection_id="parents", photos_collection_id="photos",
                configured_photo_sets_id="sets", dry_run=True, env_path=env,
                review_path=review, singletons_review_path=singletons,
            )
            self.assertEqual(client.posts, [])
            self.assertEqual(client.patches, [])
            self.assertEqual(client.field_posts, [])
            self.assertEqual(env.read_text(encoding="utf-8"), "KEEP=yes\n")
            with review.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            with singletons.open(encoding="utf-8", newline="") as handle:
                singleton_rows = list(csv.DictReader(handle))
            self.assertEqual(
                tuple(rows[0]),
                (
                    "parent_gallery_name", "parent_gallery_item_id", "photo_set_name",
                    "photo_set_slug", "photo_count", "photo_names", "photo_item_ids",
                    "source_values", "grouping_source", "grouping_confidence",
                    "is_singleton", "warnings",
                ),
            )
            self.assertEqual(len(rows), summary["unique_sets"])
            self.assertTrue(all(row["is_singleton"] == "true" for row in singleton_rows))
            first_contents = (review.read_bytes(), singletons.read_bytes())
            run_photo_sets(
                client, parent_collection_id="parents", photos_collection_id="photos",
                configured_photo_sets_id="sets", dry_run=True, env_path=env,
                review_path=review, singletons_review_path=singletons,
            )
            self.assertEqual(first_contents, (review.read_bytes(), singletons.read_bytes()))

    def test_env_update_preserves_content(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("# comment\nKEEP=yes\nTOD_PHOTO_SETS_COLLECTION_ID=old\n", encoding="utf-8")
            self.assertTrue(update_env_value_safely(env, "TOD_PHOTO_SETS_COLLECTION_ID", "new"))
            self.assertEqual(env.read_text(encoding="utf-8"), "# comment\nKEEP=yes\nTOD_PHOTO_SETS_COLLECTION_ID=new\n")

    def test_api_error_redacts_token(self):
        token = "super-secret-token"
        client = WebflowClient(token, "site", "photos", max_retries=0)
        response = Mock(status_code=400, text=f"bad Authorization: Bearer {token}", headers={})
        client.session.request = Mock(return_value=response)
        with self.assertRaises(MigrationError) as raised:
            client.request("GET", "https://example.invalid")
        self.assertNotIn(token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
