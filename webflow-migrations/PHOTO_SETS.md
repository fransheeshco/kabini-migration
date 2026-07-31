# TOD Photo Sets migration

Preview collection/field creation, grouping, item reuse, and photo links without writes:

```sh
python3 migrate.py photo-sets --dry-run
```

Optionally inspect only the first records with `--limit 10`. After reviewing the
preview, run `python3 migrate.py photo-sets`. The command discovers or creates
the collection and safely adds `TOD_PHOTO_SETS_COLLECTION_ID=<id>` to `.env`.
