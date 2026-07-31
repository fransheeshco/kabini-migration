# Repository Cleanup Report

Date: 2026-08-01

## Decision rules

The audit checked imports, CLI references, documentation references, file roles, secret patterns, sizes, and reproducibility before removal. Unknown-purpose source/data files were preserved. No source CSV, workbook, schema snapshot, field map, test, migration module, review export, or photo was deleted.

## Removed

| Path | Classification | Reason |
|---|---|---|
| `venv/` | generated | Local Python environment (27 MB); reproducible from `requirements.txt`. |
| `migration-logs/` | generated/private | API payload/response and run logs; reproducible and inappropriate for publication. |
| `dry-run-logs/` | generated | Dry-run payload/response/log output; reproducible. |
| `images-of-christ-migration/` | temporary run artifact | One-off payload, response, and log output superseded by the general workflow. |
| `migration-checkpoint.json` | generated state | Local resume state; CMS IDs and uploaded-asset state should not be published. |
| `migration-results.json` | generated state | Local migration result output. |
| `dry-run-checkpoint.json` | generated state | Reproducible dry-run state. |
| `dry-run-results.json` | generated state | Reproducible dry-run output. |
| `images-of-christ-migration-checkpoint.json` | stale checkpoint | One-gallery run state, no longer needed for operation. |
| `images-of-christ-migration-results.json` | generated result | One-gallery result output. |

All removed files were untracked in the enclosing Git repository. They are not recoverable from this folder unless another backup exists, but each is reproducible or local run state.

## Retained deliberately

| Path | Why retained |
|---|---|
| `.env` | Local operational configuration. It contains a credential-pattern finding and is ignored; revoke/rotate the token and never stage this file. |
| `PHOTO_SETS.md` | Short historical implementation note; harmless and useful context. |
| `tod_gallery_photos.csv` | Legacy/intermediate output still used as the configured fallback by `migration/config.py`. |
| `tod-gallery-photo-descriptions.csv` | Active Gallery Photo source with parsed museum metadata. |
| `todexhibit-content-v2.xlsx` | Source workbook required to reproduce extraction. |
| `collection-schema.json`, `gallery-photos-schema.json` | Exact saved schema snapshots used for offline validation and mapping review. |
| `processed-gallery-images/` and `.source` sidecars | Required prepared images and provenance; removing them would make migration inputs incomplete. |
| `photo_sets_review.csv`, `photo_sets_singletons_review.csv` | Meaningful editorial/QA outputs for handoff. |
| `extract_gallery_photos.py`, `extract_gallery_photo_descriptions.py` | Reproducibility tools referenced by the data workflow. |

## Ignore and publication policy

`.gitignore` excludes dotenv files (except the placeholder example), virtual environments, Python/test caches, logs, checkpoints/results, per-run migration directories, and temporary build folders. The final handoff PDF is not ignored. `.gitattributes` routes common image formats through Git LFS.

## Size findings

- Before cleanup: approximately 211 MB.
- After cleanup: approximately 181 MB.
- `processed-gallery-images/`: approximately 179 MB; 498 image files and 498 `.source` provenance files.
- Largest observed image: 878,491 bytes; no retained file exceeds 50 MB or GitHub's 100 MB individual-file limit.

Git LFS is recommended because of aggregate image volume, not because any one file exceeds GitHub's hard limit.
