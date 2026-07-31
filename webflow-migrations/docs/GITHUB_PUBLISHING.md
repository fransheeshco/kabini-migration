# GitHub Publishing Guide

Do not publish until the previously exposed Webflow token has been revoked and replaced.

## Pre-publish checklist

1. Review `docs/CLEANUP_REPORT.md` and `git status --short` from the enclosing repository root.
2. Revoke the old token in Webflow, create a replacement with minimum required access, and update only local `.env`.
3. Confirm `git check-ignore -v webflow-migrations/.env` reports an ignore rule.
4. Install Git LFS and confirm image attributes.
5. Run tests, CLI help, and a narrow authorized dry run.
6. Check size and large files.
7. Scan the exact staged set for secrets before committing.

```bash
cd /Users/francee/mata-internship/kabini
git status --short -- webflow-migrations
git check-ignore -v webflow-migrations/.env

git lfs install
git -C webflow-migrations check-attr filter -- processed-gallery-images/in-love-with-mary/photo-079--del-patrocinio-de-maria.jpg

cd webflow-migrations
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python3 migrate.py --help
du -sh . processed-gallery-images
find . -type f -size +90M -print
```

After setting a rotated token locally, the safest live-connected check is read-only:

```bash
python3 migrate.py validate --scope all
python3 migrate.py dry-run --scope galleries --slug images-of-christ --limit 1 --batch-size 1
python3 migrate.py photo-sets --dry-run
```

These commands read Webflow. Do not run `migrate` or `photo-sets` without `--dry-run` during publication review.

## First commit

The Git root is the parent `kabini` directory and already contains unrelated changes. Stage only this subdirectory, then inspect the staged set carefully:

```bash
cd /Users/francee/mata-internship/kabini
git add webflow-migrations
git status --short -- webflow-migrations
git diff --cached --stat -- webflow-migrations
git diff --cached -- . ':(exclude)webflow-migrations/processed-gallery-images/**'
git lfs ls-files
```

Run a secret scanner if available (for example Gitleaks) against the staged/repository content. At minimum, confirm `.env` is absent:

```bash
git diff --cached --name-only | grep -E '(^|/)\.env$' && echo 'STOP: .env staged' || echo '.env not staged'
git commit -m "Prepare Webflow migration project handoff"
```

## Create and push the remote

Create an empty private repository in GitHub first. Then run, substituting the real owner/repository and current branch:

```bash
git remote add origin git@github.com:OWNER/REPOSITORY.git
git branch --show-current
git push -u origin YOUR_BRANCH
```

Do not add a second `origin` if the enclosing repository already has one. In that case, decide whether this folder belongs in the existing repository or should be split into a standalone repository before committing. Verify the GitHub file list, LFS objects, README rendering, and absence of `.env`, logs, checkpoints, tokens, and API response files after push.

If full production photos should not live in the main repository, choose and document one controlled alternative: a separate Git LFS repository, a release archive, or cloud storage with checksum/restore instructions. A reduced sample dataset is suitable for demonstration only; production migration still requires the complete local folder.

