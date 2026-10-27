"""Backfill missing per-user category prefs at level 3 (issue #77).

The server runs the same backfill on startup, so a normal restart applies it.
Run this during a deploy to preview the change first, or to apply it without
restarting:

    python scripts/migrate-category-prefs.py --dry-run
    python scripts/migrate-category-prefs.py

The database defaults to $CLEANPLEX_DATA/cleanplex.db (~/.cleanplex otherwise),
matching the server. Back it up before applying, as for any deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from cleanplex import database as db


async def _run(db_path: Path, dry_run: bool) -> None:
    db.set_db_path(db_path)
    missing = await db.get_missing_user_category_prefs()
    if not missing:
        print(f"{db_path}: nothing to backfill")
        return

    by_user: dict[str, list[str]] = {}
    for row in missing:
        by_user.setdefault(row["plex_username"], []).append(row["category"])
    for user, categories in by_user.items():
        print(f"  {user}: {', '.join(categories)} -> level {db.DEFAULT_CATEGORY_LEVEL}")

    if dry_run:
        print(f"Dry run: {len(missing)} rows would be added for {len(by_user)} users")
        return
    added = await db.backfill_user_category_prefs()
    print(f"Added {added} rows for {len(by_user)} users")


def main() -> None:
    default = Path(os.environ.get("CLEANPLEX_DATA", Path.home() / ".cleanplex")) / "cleanplex.db"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=default, help=f"database file (default: {default})")
    parser.add_argument("--dry-run", action="store_true", help="report what would change and exit")
    args = parser.parse_args()
    if not args.db.exists():
        parser.error(f"database not found: {args.db}")
    asyncio.run(_run(args.db, args.dry_run))


if __name__ == "__main__":
    main()
