# Hayaa sync: test scenarios

Integration-level expected behaviour of the Hayaa client (`cleanplex/hayaa*.py`, docs/SYNC_SERVICE.md).
Unit coverage: `tests/unit/test_hayaa.py`, `test_hayaa_sync.py`, `test_hayaa_verify.py`. End to end: two
instances (separate data dirs and accounts) against `python -m hayaa.devserver` in the Hayaa repo.

| # | Setup | Action | Expected |
|---|---|---|---|
| 1 | A linked, title scanned with one detection | A syncs | Media descriptor and one `detect` sent; A's segment linked to the new claim |
| 2 | B linked, same file, scan found nothing, Balanced | B syncs | B sends `clean`; claim is `candidate`, not applied, re-check pending, listed under review |
| 3 | After 2 | B keeps the claim | Pulled segment applied; next sync sends `confirm`; claim becomes `accepted` (2 supporters, basis both) |
| 4 | After 3 | A syncs | Claim linked to A's own segment; nothing duplicated |
| 5 | After 3 | B deletes the pulled segment, syncs twice | Never comes back (`hayaa_overrides` delete); B's `confirm` is retracted |
| 6 | After 4 | A marks its segment false positive, syncs | `detect` retracted, `reject` sent, no `clean` |
| 7 | Any | Unmark the false positive | `reject` retracted; `detect` sent again under a new generation id |
| 8 | Any | Rescan moves a segment | Old `detect` retracted, new one sent; pulled segments re-applied after the scan |
| 9 | Imported skip-file segments | Sync | Nothing sent for them |
| 10 | Claim `rejected`, local medium detection | Sync | Local segment hidden (`false_positive = 3`), listed as community false positive; Restore keeps it shown |
| 11 | Claim `rejected`, local high-severity detection | Sync | Kept unless the local false-positive model agrees |
| 12 | Service answers 429/503 with Retry-After | Sync | Run stops, pause recorded, no retry until it passes; automatic sync skips |
| 13 | Automatic sync on | Quota at 80% | Automatic run stops before the quota; manual run may use the rest |
| 14 | 20+ re-checked machine-only accepted claims, >10% misses | Sync | Preset switches to Verify first and the UI says so |
