#!/usr/bin/env python3
"""Command-line entry point for the Webflow migration."""

from __future__ import annotations

import logging
import sys

from migration.runner import MigrationError, main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.error("Migration interrupted by user.")
        raise SystemExit(130)
    except MigrationError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
    except Exception:
        logging.exception("Unexpected migration failure.")
        raise SystemExit(2)
