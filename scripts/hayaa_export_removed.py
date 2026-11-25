"""One-off export of removed titles' scans for a Hayaa maintainer import by release key (#228).

Titles scanned and then deleted before any Hayaa sync have no content key (only the file can
produce one). Their file name and size, kept in keyframe_index until the purge, give the release
key; Hayaa's admin action `import_releases` stages the scans under it and files them once any
server describes a file with that release key.

Reads a *copy* of the database and never writes it. Only machine detections are exported:
removed titles from before #228 have no scan status, so a clean result cannot be told apart from
a title that was never scanned.

    uv run python scripts/hayaa_export_removed.py <db copy> <out.json> [--since 2026-11-01]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from cleanplex import database as db
from cleanplex import hayaa
from cleanplex import hayaa_sync as hs


async def export(db_path: Path, since: str) -> tuple[list[dict], dict[str, int]]:
    db.set_db_path(db_path)
    await db.init_db()  # brings an older copy's schema up to date (the copy, never the live file)
    detector = await hs._nudenet_detector()
    async with db.get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT r.plex_guid, r.title, r.file_path, COALESCE(r.duration_ms, 0) AS duration_ms, k.size "
            "FROM removed_titles r JOIN keyframe_index k ON k.file_path = r.file_path "
            "WHERE r.removed_at >= ? AND COALESCE(r.file_path, '') != '' "
            "AND COALESCE(json_array_length(NULLIF(r.part_files, '')), 0) <= 1",
            (since,),
        )
    titles = [dict(r) for r in rows]
    segs: dict[str, list[dict]] = {}
    for s in await db.get_removed_segments_for_hayaa([t["plex_guid"] for t in titles]):
        segs.setdefault(s["plex_guid"], []).append(s)

    items, stats = [], {"titles": len(titles), "exported": 0, "no_detections": 0, "observations": 0}
    for t in titles:
        rk = hayaa.release_key(Path(t["file_path"]).name, int(t["size"]))
        job = {"plex_guid": t["plex_guid"], "duration_ms": t["duration_ms"], "status": "", "scan_version": 0}
        want = hs.desired_observations(rk, job, segs.get(t["plex_guid"], []), [], {}, detector)
        obs = [{k: v for k, v in {"id": base, **o}.items() if k != "title"}
               for base, o in sorted(want.items()) if o["kind"] == "detect"]
        if not obs:
            stats["no_detections"] += 1
            continue
        items.append({"release_key": rk, "observations": obs})
        stats["exported"] += 1
        stats["observations"] += len(obs)
    return items, stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("db", type=Path, help="a copy of cleanplex.db (read only)")
    p.add_argument("out", type=Path)
    p.add_argument("--since", default="1970-01-01", help="only titles removed on or after this date")
    a = p.parse_args()
    items, stats = asyncio.run(export(a.db, a.since))
    a.out.write_text(json.dumps({"items": items}))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
