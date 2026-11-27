# Scanner Queue & Priority

The queue is stored in the database, so the Dashboard, the Browse page and the scan workers all see the same list, and it survives restarts. Each worker claims the next title from the database in one step, so a title is never scanned twice and a title being scanned is never picked up again.

- **Order** — Movies newest first, then TV shows grouped so a show's episodes run together
- **Force priority** — Titles you **"Scan Now"** always run before everything else, and may run outside the scan window
- **Self-healing** — Any pending title the scanner is allowed to scan (not ignored, library not excluded, rating in your filter) is queued automatically, whichever way it reached the database

`GET /api/scan/queue` returns the unfinished jobs (finished ones are left out unless you pass `status=`), with `limit`/`offset` paging and per-status `counts`.

## When you click "Scan Now"

1. The title gets the force-scan flag and moves to the front of the queue
2. A scanner worker claims it immediately (force titles are claimed first)
3. It scans regardless of the configured scan window time restrictions
