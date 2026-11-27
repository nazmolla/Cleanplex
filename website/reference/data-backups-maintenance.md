# Data, Backups & Maintenance

Everything lives in the data folder (`~/.cleanplex`, or `CLEANPLEX_DATA`):

| Path | What | Housekeeping |
|---|---|---|
| `cleanplex.db` | The database (SQLite, WAL) | A consistent backup is written to `backups/` once a day (5 minutes after startup if none is under a day old); the newest 7 are kept. Manual `cleanplex.db.bak-*` copies are trimmed to the newest 3 |
| `thumbnails/`, `hover/`, `feedback/` | Segment thumbnails, hover previews, false-positive frames | Removed with their segment; a sweep at startup and every 6 hours removes unreferenced files |
| `posters/` | Cached Plex posters | Refreshed after 7 days; the sweep deletes ones untouched for 30 days |
| `previews/` | On-demand segment preview clips | The sweep deletes clips older than 14 days and the oldest first once the folder passes 500 MB; they are re-rendered when played |
| `models/` | NudeNet 640m, SigLIP and the verifier | Kept |
| `hayaa/` | Hayaa sync signing key and sign-in token | Kept with `0600` permissions; excluded from database backups |
| `cleanplex.log` | Log, only when `CLEANPLEX_LOG_FILE` is set | Rotated at 10 MB, 4 backups kept |

## Library sync & restoring backups

New titles are discovered by a library pass every 5 minutes (one chunked lookup per library, not one per title).

To restore a backup:

1. Stop the Hayaa for Plex / Cleanplex service
2. Copy a backup file from `backups/` over `cleanplex.db`
3. Start the service again
