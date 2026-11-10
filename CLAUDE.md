# Cleanplex — Mandatory Development Practices

This file is the authoritative source for how all work on this repository must be done.
Every change — regardless of size — must follow these rules. No exceptions.

---

## Table of Contents

1. [Tech Stack & Architecture](#1-tech-stack--architecture)
2. [Issue Lifecycle](#2-issue-lifecycle)
3. [Code Comments](#3-code-comments)
4. [Documentation Standards](#4-documentation-standards)
5. [Architecture Rules](#5-architecture-rules)
6. [Test Coverage](#6-test-coverage)
7. [Pull Request & Commit Standards](#7-pull-request--commit-standards)
8. [Issue Closing Protocol](#8-issue-closing-protocol)
9. [AI-Assisted Development](#9-ai-assisted-development)
10. [Deploy Workflow](#10-deploy-workflow)

---

## 1. Tech Stack & Architecture

| Layer | Technology | Notes |
|---|---|---|
| Backend | Python 3.11+, FastAPI, aiosqlite | All I/O must be async |
| Database | SQLite (WAL mode) | Single file at `~/.cleanplex/cleanplex.db` |
| ML Inference | NudeNet (ONNX, local) | CPU-only; wrapped in `asyncio.to_thread` |
| ML Fallback | SigLIP base 224 (ONNX via onnxruntime, `tokenizers`) | Optional; only on frames NudeNet did not flag |
| Content vocabulary | MovieContentFilter 1.1.0 | Category names, severities and channels; see `importers/_common.py` |
| Video | FFmpeg / ffprobe | Subprocess via `frame_extractor.py` |
| Plex API | `plexapi` + `httpx` | Wrapped in `PlexClient` |
| Frontend | React, TypeScript, Vite, Tailwind | SPA served from `frontend/` |
| Tests | `pytest`, `pytest-asyncio` | See §6 |

**Module ownership:**

| Module | Responsibility |
|---|---|
| `database.py` | All SQL — no raw queries outside this file |
| `scanner.py` | Frame extraction, NudeNet inference, segment writing, and the queue API (`enqueue`, `force_scan_job`, `rebuild_queue`, `sync_queue`) over the DB-backed scan queue |
| `siglip_detector.py` | SigLIP zero-shot fallback for frames NudeNet did not flag — model download, prompts from `scan_labels`, scoring |
| `verifier.py` | Feedback-trained false-positive filter (#165): trains a logistic head on SigLIP image embeddings of the frames users marked as false positives versus the thumbnails of kept segments, one model per detector family and label (`siglip:BUTTOCKS_EXPOSED`; a label without enough accepted and rejected examples has no model and is never rejected, #178), and judges a flagged cluster's peak frame at scan time. Only filters detections; never creates segments. A rejected cluster is stored with `segments.false_positive = 2` (`db.FP_VERIFIER`): inactive like a user mark (1), shown in the false-positive list, restorable, and excluded from accuracy rates and from training as a kept example |
| `cast_sync.py` | Background job (#180): copies each movie's and show's cast from Plex (`PlexClient.get_cast`, chunked metadata requests) into `title_cast`; episodes use their show's cast. Drains unsynced titles in batches, re-syncs after 30 days, never runs inside the library pass |
| `ort_runtime.py` | Shared onnxruntime session options: caps threads per inference session so scan workers do not oversubscribe the CPU. Every `InferenceSession` the scanner or SigLIP builds goes through it |
| `stream_proxy.py` | The enforcement proxy (#132, #184), run by `main.py` on `CLEANPLEX_PROXY_PORT` behind the front door. For a filtered viewer and a title with segments their levels skip: refuses direct play, gets an HLS stream (video copied when the file's keyframes are indexed, else re-encoded), removes the overlapping segments from the playlist, re-times subtitles, maps progress back, records played cuts as skip events, and fails closed (403) on original files, downloads and non-HLS streams. Sessions persist in `proxy_sessions`. Only decisions, playlists, subtitles and progress reach it; nginx streams all bytes |
| `frontdoor.py` | Front door (#184): extracts Plex's `plex.direct` certificate (cert-v2.p12, password SHA-512 of "plex"+ProcessedMachineIdentifier) per OS, renders the nginx config (TLS/plain split on one port, PROXY protocol, X-Forwarded-For only for public client addresses), and reloads nginx when Plex renews the certificate. Installed by `scripts/frontdoor/install.sh` (pf anchor `com.apple/cleanplex` on macOS, nftables on Linux) |
| `container.py` | Docker entrypoint (#194): seeds the Plex URL/token from env or Plex's Preferences.xml, runs the app as a child, supervises nginx (restarted if it dies, started once Plex's certificate exists) and, once, enables enforcement and turns Plex Relay/DLNA off through the app's own API. Marker `frontdoor/configured` stops a restart overriding later user choices. Image files: `Dockerfile`, `docker/entrypoint.sh` (root-only setup, optional nftables redirect, drops to Plex's file owner), compose files, `scripts/docker-smoke.sh` (CI runs it) |
| `keyframe_index.py` | Background job indexing video keyframes of flagged single-file titles (ffprobe, one file per minute) into `keyframe_index`, so the proxy can cut copied streams: in copy mode Plex starts a segment on a keyframe near its nominal time |
| `plex_summary.py` | The Cleanplex block in a title's Plex summary: builds the description from the database (`db.get_summary_stats`) and refreshes it (debounced) after scans, deletes, false positives, relabels, imports and subtitle scans. Plex writes go through `PlexClient.update_cleanplex_summary` |
| `segment_refine.py` | Dense sampling of a flagged window (`dense_hits`): the frames above threshold inside it, growing while the scene runs on. Shared by the scanner and the backfill, so both choose thumbnails and bounds the same way |
| `thumbnail_backfill.py` | Background job (scan window, one segment at a time) that re-samples segments stored before dense sampling existed: better thumbnail, bounds that only grow, `refined_at` so each is processed once, hover preview reset when the window changes |
| `segment_files.py` | Files that belong to segments (thumbnails, hover previews, feedback frames) plus the poster and preview-clip caches (pruned by age/size): deletes them with their segment (`delete_segment`, `delete_segments_for_guid`: use these, never `db.delete_segment*` directly) and sweeps orphans on a schedule |
| `db_backup.py` | Daily `VACUUM INTO` backup to `~/.cleanplex/backups/` (newest 7 kept) and pruning of deploy-time `cleanplex.db.bak-*` copies (newest 3 kept) |
| `hover_previews.py` | Job rendering each active segment's hover preview (small silent MP4) inside its own window (default 00:00–07:00); separate from the scanner, never part of a scan |
| `subtitle_scanner.py` | Subtitle parsing and profanity matching — emits muted language segments |
| `importers/` | Skip file parsers and exporters (`.skp`, `.edl`, `.mcf`, pasted lists). One module per format; all return the same normalized segment dict |
| `plex_client.py` | All Plex API calls — no `plexapi` imports elsewhere |
| `filter_engine.py` | Playback position checks and seek decisions (live skipping). Stands down for streams the proxy edited (`db.is_proxied`); seeks to the widened segment end (#185) |
| `watcher.py` | Polling loops only — no business logic. The library pass runs every 5 min and must look titles up in chunks, never per item. The session loop also runs the enforcement backstop (stops a filtered stream that bypassed the front door, only when `enforcement_enabled`) |
| `sync.py` / `sync_merge.py` | GitHub sync and segment merge logic |
| `bg_jobs.py` | Background job lifecycle — no domain logic |
| `events.py` | In-process event bus streamed to browsers by `GET /api/events` (SSE). Publish where a change happens; events name what changed, not the new state |
| `web/routes/` | HTTP surface only — call domain modules, never raw DB |

---

## 2. Issue Lifecycle

### 2.1 When to Create an Issue

Create a GitHub issue **before writing any code** for:

- Any bug, regression, or incorrect behaviour
- Any performance problem (N+1, blocking call, unbounded loop, etc.)
- Any reliability or safety concern (race condition, resource leak, etc.)
- Any tech-debt item or refactor that touches more than one file
- Any new feature or behaviour change

**Skip the issue** only for:
- Typos in comments or strings
- Formatting-only changes that touch no logic

### 2.2 Required Issue Fields

Every issue must have:

**Title** — `<Verb> <what> in <where>` — specific, under 72 characters.
Good: `Batch user filter resolution in sessions endpoint`
Bad: `Fix performance`

**Labels** — pick all that apply:

| Label | When |
|---|---|
| `bug` | Incorrect observable behaviour |
| `performance` | Throughput, latency, or resource use |
| `reliability` | Race condition, leak, crash, data loss risk |
| `tech-debt` | Maintainability or refactor |
| `caching` | Cache add/fix/invalidation |
| `backend` | Python / FastAPI / SQLite |
| `frontend` | React / TypeScript |
| `high-priority` | Blocks users or corrupts data |
| `medium-priority` | Degrades experience, workaround exists |
| `low-priority` | Minor, cosmetic, or nice-to-have |

**Body template** (all five sections required):

```markdown
## Summary
One or two sentences describing the problem.

## Evidence
- file/path.py:LINE — what the line does and why it's a problem

## Impact
Concrete consequence: what breaks, slows down, or degrades.

## Proposed Work
- Bullet list of specific changes. Not a full design, but enough to start.

## Acceptance Criteria
- Testable, observable outcomes that define "done".
- Written as assertions, not tasks.
```

### 2.3 Issue Triage

Before starting any work, confirm:
- The issue is reproducible (or clearly derivable from code)
- The acceptance criteria are complete and testable
- No duplicate issue exists (`gh issue list --state open` before creating)

---

## 3. Code Comments

### 3.1 What Must Be Commented

Comment when the logic is **not self-evident** from the code itself.

**Always comment:**

- Non-obvious algorithmic choices (`# O(n²) — acceptable for <50 segments per title`)
- Intentional trade-offs (`# Update every 30 steps to reduce write amplification`)
- Why something is done in a surprising way (`# threading.Lock, not asyncio.Lock — detector runs in a thread pool`)
- Magic numbers (`LOOKAHEAD_MS = 5000  # compensates for 5s polling latency`)
- Async/thread-safety notes (`# guarded by _state_lock — do not mutate outside _update_state()`)
- Fallback/retry logic rationale
- Any `# type: ignore` or `# noqa` — must explain why

**Never comment:**
- What the code obviously does (`# increment counter`)
- Commented-out code — delete it; git history preserves it
- TODO/FIXME — open a GitHub issue instead

### 3.2 Module-Level Docstrings

Every Python module (`*.py`) must start with a one-line docstring:

```python
"""Short description of what this module owns."""
```

Multi-line is fine when the module has non-obvious scope or invariants.

### 3.3 Function/Method Docstrings

Required for:
- Every `async def` in `database.py` (documents query, params, return shape)
- Every `public` method in `PlexClient`
- Every FastAPI route handler

Not required for small private helpers where the name is fully self-explanatory.

Format — keep it short:

```python
async def get_segments_for_guid(plex_guid: str) -> list[dict]:
    """Return all segments for the given Plex GUID, ordered by start_ms."""
```

Only add parameter/return docs when the types or semantics are non-obvious.

### 3.4 Frontend Comments

TypeScript/React: comment the same categories as Python above.
Add a comment above any `useEffect` hook that has a non-trivial dependency array explaining the intent.

---

## 4. Documentation Standards

### 4.1 README.md

`README.md` is the user-facing reference. Update it when:

- A new configuration setting is added (add a row to the Configuration table)
- A new environment variable is added
- A new web UI page or feature is added
- A significant behaviour changes (e.g., skip logic, segment expansion)
- A new client compatibility result is known

Do **not** add implementation details or architecture notes to `README.md` — that goes in `CLAUDE.md` or inline code comments.

### 4.2 CLAUDE.md (this file)

Update this file when:

- A new module is added (add a row to §1 module table)
- An architectural rule changes
- A new mandatory practice is established
- A label is added to the GitHub label set

### 4.3 PHASE1_TEST_SCENARIOS.md and similar test documents

These scenario files describe integration-level expected behaviour. Update them when:
- A new sync/merge behaviour is implemented
- Edge cases are discovered and fixed
- A scenario's expected result changes

### 4.4 Changelogs

No `CHANGELOG.md` is maintained. The git log and closed GitHub issues serve as the changelog. Write commit messages and issue close comments accordingly (see §7 and §8).

---

## 5. Architecture Rules

These rules are invariants. Breaking them requires an explicit discussion issue before the PR.

### 5.1 Async Discipline

- **All I/O is async.** `aiosqlite`, `httpx.AsyncClient`, `asyncio.to_thread` for blocking libs.
- **Never block the event loop.** No `time.sleep`, synchronous file reads >1 KB, or synchronous network calls in the async path.
- **Use `asyncio.Lock` for shared async state.** Never use `threading.Lock` in code that runs on the event loop.
- **Wrap all `plexapi` calls in `asyncio.to_thread`.** The `plexapi` library is synchronous.

### 5.2 Database Layer

- **All SQL lives in `database.py`.** No raw SQL strings in routes, scanner, sync, or any other module.
- **Never load full rows to count.** Use `SELECT COUNT(*)` — never `len(await db.get_all_x())`.
- **No N+1 queries.** If a route or function calls the DB once per item in a list, it is a bug. Use `IN (...)`, `JOIN`, or batch queries.
- **Schema migrations are additive only.** Never drop or rename columns in a migration. Add new columns with defaults.
- **Index every foreign-key-like column** (`plex_guid`, `file_hash`, `status`).

### 5.3 HTTP & Plex Client

- **One `AsyncClient` per `PlexClient` instance.** Never construct `httpx.AsyncClient` ad-hoc inside a loop or per-request.
- **Always close `AsyncClient` on shutdown.** Wire `client.close()` into the application lifecycle.
- **Retry logic must have a ceiling.** Every retry loop must have a max-attempt count and exponential backoff or fixed cap.
- **No Plex API calls in loops without caching.** Calls like `get_episode_show_art` must be memoized within the request at minimum.

### 5.4 Content Classification

- **Categories come from the MovieContentFilter 1.1.0 vocabulary.** Never invent a category name; map onto an existing group or use `other`.
- **Every segment carries `category`, `severity`, `action`, `channel` and `source`.** Importers set them from the file; the scanner uses its defaults.
- **A detector segment's severity comes from its labels** via `severity_for_labels` / `LABEL_SEVERITY` in `importers/_common.py` (exposed genitalia and anus high, exposed breast and buttocks medium, everything else low). Never hardcode a scanner severity; imported segments keep the file's.
- **Only `skip` and `mute` are executable.** Plex exposes playback position and volume, nothing else. `blank` and `blur` must be logged and ignored, never downgraded to a skip.
- **Filtering is a threshold, not a boolean**: a segment fires when the user's category level plus its severity rank exceeds 3.
- **A new parser goes in `importers/`** and must return normalized segment dicts through `validate_segment`. Never let a parser write to the database itself.
- **Never redistribute third-party segment data.** Parse what a user supplies; do not bundle or mirror another project's catalogue.

### 5.5 Scanner

- **One NudeNet detector per thread.** Use `threading.local()` — never instantiate inside a tight frame loop.
- **Scanner global state (`_queued_guids`, `_current_scan_guids`, `_paused`) is guarded by a lock** before mutation.
- **Bump `scanner.SCAN_VERSION` when a change means existing scan results are worth redoing** (thresholds, prompts, severity, thumbnails, bounds). It is stored per finished job so "rescan outdated titles" can find what an older scanner produced.
- **Flagged windows are re-sampled densely before insert** (`_refine_cluster`): the thumbnail is the peak frame, bounds only grow, and a failure leaves the coarse result. A segment's kept-or-dropped decision stays with the coarse pass.
- **Segments are clustered before DB insert.** Never insert a raw frame-level row; always cluster via `_cluster_frames` / `_flush_cluster`.

### 5.5a Scan Queue

- **The scan queue lives in the database** (`scan_jobs.queue_pos`: NULL = not queued, lower runs first, `force_scan` first). Never keep queue membership or order in process memory.
- **Workers take work only through `db.claim_next_scan_job`**, one atomic `UPDATE ... RETURNING` that sets `status='scanning'`. Never select-then-update, so two workers cannot claim the same title and a scanning title cannot be claimed.
- **Every queue change writes the DB** (`enqueue`, `enqueue_many`, `force_scan_job`, `db.release_scan_job`, `db.dequeue_scan_jobs`, `db.set_ignored`); `done`/`failed`/reset clear the position. `scanner._queue_cond` is only a wake-up hint.
- **Views read the DB**: queue size, order and "currently scanning" come from `db.count_queued_scan_jobs`, `db.get_queued_scan_jobs`, `db.get_scanning_guids`. A new insert path needs no enqueue call to be correct: idle workers run `sync_queue()`, which queues every pending, eligible, unqueued title.

### 5.6 Frontend

- **All API calls go through `src/api/`.** No `fetch`/`axios` calls inline in components or pages.
- **Polling intervals must cancel in-flight requests.** Use `AbortController` before each new poll tick.
- **No polling loop may run unbounded.** Every `setInterval`/recursive `setTimeout` must have a terminal condition (success, error, or max-duration).
- **Bulk actions must use bounded concurrency or a batch endpoint.** Never fire N sequential requests from a UI loop.

### 5.7 Sync

- **Sync is always manually triggered.** No background task or watcher may initiate a sync operation automatically.
- **File hash cache is keyed by `(path, size, mtime)`.** Recompute SHA256 only when any of these change.
- **Segment merge complexity must be sub-quadratic on practical inputs.** Use time-bucketed or sorted interval matching.

### 5.8 Security & Resource Hygiene

- **Never return secrets from the API.** `GET /api/settings` masks `plex_token` (`plex_token_set` instead) and a blank token on save means unchanged; CORS is same-origin unless `CLEANPLEX_CORS_ORIGINS` is set.
- **Close async generators you stop iterating early.** Wrap ffmpeg frame streams in `contextlib.aclosing` so a pause, skip or error closes them at once instead of at shutdown.


### 5.9 Stream Proxy & Front Door

- **Video bytes never pass through Python.** nginx streams segments and files from Plex; the Python proxy only sees decisions, starts, playlists, subtitles and progress, and answers nginx `auth_request` yes/no checks (`/_proxy/authorize`, `/_proxy/segment`).
- **Fail closed for a filtered viewer and a flagged title**: refuse the original file, downloads, non-HLS streams and removed segments (403). Preview thumbnails (`/indexes/`) are deliberately allowed.
- **X-Forwarded-For carries only public client addresses.** nginx replaces whatever the client sent; LAN clients get none, because Plex ignores a private address there and marks the viewer remote.
- **nginx passes `Host $http_host`**, never `$host`: Plex builds redirect URLs from it and `$host` drops the port.
- **Redirect to the interface's own address, never to loopback**: macOS drops other machines' traffic redirected to 127.0.0.1 (outage 2026-11-05).
- **Copy mode only with a matching keyframe index**: one Plex part for the title (`title_parts`) and an unchanged file; otherwise Plex re-encodes. Cut decisions use the conservative keyframe bounds in `edit_playlist`.
- **The backstop acts only when `enforcement_enabled` is set** (by the installer), and a dev instance must keep it off — it watches the same Plex.

---

## 6. Test Coverage

### 6.1 Test Location & Structure

```
tests/
├── unit/
│   ├── test_database.py       # DB helpers — real in-memory SQLite
│   ├── test_sync_merge.py     # SegmentMerger logic
│   ├── test_filter_engine.py  # FilterEngine seek decisions
│   └── test_plex_client.py    # PlexClient (httpx mocked)
├── integration/
│   ├── test_routes_segments.py
│   ├── test_routes_sessions.py
│   └── test_routes_scanner.py
└── conftest.py                # shared fixtures
```

### 6.2 What Requires a Test

| Change type | Required coverage |
|---|---|
| New `database.py` function | Unit test: happy path + empty/missing input |
| New route handler | Integration test: happy path + 404/422 error cases |
| New sync / merge logic | Unit test: correctness + edge cases (empty, single source, conflict) |
| Bug fix | Regression test that fails before the fix and passes after |
| Performance optimisation | Before/after assertion (row count, call count, or timing) |
| New filter / skip logic | Unit test covering the boundary conditions |

### 6.3 Test Standards

- **Use real in-memory SQLite for database tests** — never mock the DB.
- **Mock `httpx` responses** for Plex API tests using `httpx.MockTransport` or `respx`.
- **Mock `plexapi`** at the boundary (`asyncio.to_thread` call site) — never import `plexapi` in test files.
- **Test names describe the scenario**: `test_get_segments_returns_empty_for_unknown_guid`, not `test_get_segments`.
- **Each test is independent** — no shared mutable state between tests; use fixtures.
- **`pytest-asyncio` mode: `asyncio_mode = "auto"`** (set in `pyproject.toml` or `pytest.ini`).

### 6.4 Running Tests

```bash
pytest tests/ -v
```

Tests must pass before any PR is merged. A PR that breaks existing tests will not be merged regardless of other quality.

### 6.5 Coverage Threshold

- Backend: ≥ 80% line coverage on `cleanplex/` (excluding `main.py`).
- New modules must hit 80% on first PR.
- Use `pytest --cov=cleanplex --cov-report=term-missing` to verify.

---

## 7. Pull Request & Commit Standards

### 7.1 Commit Messages

Format: `<type>: <what changed in imperative mood>`

| Type | When |
|---|---|
| `feat` | New user-visible feature or behaviour |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `refactor` | Code restructuring with no behaviour change |
| `test` | Test-only changes |
| `docs` | Documentation only |
| `chore` | Build, dependencies, tooling |

Rules:
- Subject line ≤ 72 characters
- Use body to explain **why**, not **what** (the diff shows what)
- Reference the issue number: `Closes #12`, `Part of #9`

Examples:
```
perf: batch user filter lookups in sessions endpoint

Eliminates N+1 DB calls when multiple sessions are active.
Each request now loads all user filters once and maps in-memory.

Closes #11
```

```
fix: use asyncio.Lock for model download gate in scanner

threading.Lock blocks the event loop when the download path is reached
from an async context, causing session polling to stall.

Closes #3
```

### 7.2 PR Requirements

Before opening a PR:

- [ ] All existing tests pass (`pytest tests/ -v`)
- [ ] New tests added for the change (see §6.2)
- [ ] Coverage does not regress below threshold
- [ ] `README.md` updated if user-visible behaviour changed
- [ ] `CLAUDE.md` updated if a new architecture rule or module was introduced
- [ ] Issue number referenced in the PR description
- [ ] PR description includes "Closes #N" for every issue the PR resolves

PR title mirrors the commit type:
`perf: batch user filter lookups in sessions endpoint`

---

## 8. Issue Closing Protocol

An issue is closed **only** when every acceptance criterion is met and verified.

### 8.1 Closing Comment (required)

When closing an issue — whether via a merged PR or direct close — post a comment in this format:

```
## Resolution

**Status:** Fixed / Won't Fix / Duplicate / No Longer Applicable

### What was done
- Specific change 1 (file:line)
- Specific change 2 (file:line)

### How to verify
- Step or assertion that confirms the fix works.
- For performance issues: include before/after metric or log evidence.

### Related
- PR #N
- Depends on / blocks #N (if applicable)
```

### 8.2 Close Reasons

| Reason | When | Required action |
|---|---|---|
| Fixed | Code merged, criteria met | Closing comment with PR link |
| Won't Fix | Out of scope or intentional design | Closing comment explaining rationale |
| Duplicate | Another issue covers it | Link to canonical issue; close this one |
| No Longer Applicable | Underlying code removed or superseded | Closing comment with explanation |

### 8.3 Never Close Without

- A closing comment (even for Won't Fix)
- Verifying all acceptance criteria from the issue body
- Confirming tests pass (for Fixed issues)

---

## 9. AI-Assisted Development

When using Claude Code or any AI assistant on this repository:

### 9.1 Before Starting Work

- Always check open issues first: `gh issue list --state open`
- Do not create duplicate issues — search before filing
- Read the relevant source files before proposing changes
- Do not write code for something not tracked in an issue (unless it's a typo)

### 9.2 When Writing Code

- Follow all rules in §5 Architecture Rules without exception
- Do not add helpers, abstractions, or error handling beyond what the issue requires
- Do not refactor surrounding code unless the issue explicitly calls for it
- Reference the issue number in every commit message

### 9.3 When Filing Issues

- Use the template in §2.2 exactly
- Evidence lines must be real file:line references verified by reading the code
- Proposed work must be concrete enough that any developer can start without clarification
- Do not batch unrelated problems into one issue

### 9.4 When Closing Issues

- Post the closing comment from §8.1 before or immediately after the close
- If a fix introduces a new issue (common with performance work), file it before closing the original
- If code in the issue evidence has moved (line numbers shifted), update the closing comment with the current location

---

## 10. Deploy Workflow

**Every change that touches Python or frontend code must pass through a dev instance before touching production.** No exceptions — not for "trivial" fixes, not for one-liners.

### 10.1 Dev Instance

The dev instance runs on port **7980** with data dir `~/.cleanplex-dev/`. It is seeded from the production DB on first start so Plex credentials and settings are pre-configured.

Scripts:

```bash
bash scripts/dev-start.sh        # seed DB from prod (if needed) and start on :7980
bash scripts/dev-verify.sh       # smoke-test all API endpoints
bash scripts/dev-stop.sh         # stop the dev instance
```

`dev-start.sh --fresh` wipes `~/.cleanplex-dev/` and reseeds from production. The scripts run on macOS,
Linux and Windows (Git Bash); `CLEANPLEX_PROD_DB` points at the production database when it is another
account's (on the Mac Mini: `/Users/server/.cleanplex/cleanplex.db`, copied with sudo). The dev instance runs
no stream proxy and always has the enforcement backstop switched off.

### 10.2 Mandatory Pre-Deploy Checklist

Before building the frontend or restarting the production server:

1. **Run unit tests**: `pytest tests/ -v` — all must pass
2. **Start dev instance**: `bash scripts/dev-start.sh`
3. **Verify startup log** — check `cleanplex-dev.log` for any exceptions or `AttributeError`
4. **Run smoke tests**: `bash scripts/dev-verify.sh` — all endpoints must return 200
5. **Exercise changed paths manually** — if a scanner change was made, verify a scan job runs; if UI changed, load the page in a browser at `http://localhost:7980`
6. **Stop dev instance**: `bash scripts/dev-stop.sh`
7. **Build frontend**: `cd frontend && npm run build`
8. **Deploy to production**: restart the production server

Steps 3–5 catch runtime errors (missing attributes, import failures, startup crashes) that unit tests cannot catch because they don't boot the full server.

### 10.3 Production Restart Procedure

**Production runs on the Mac Mini (192.168.0.80) as a LaunchDaemon**, `com.homeserv.cleanplex`,
under the `server` account, from `/opt/cleanplex` (installed with uv, Python 3.12). Data and log live in
`/Users/server/.cleanplex/`. Deploying copies the working tree to `/opt/cleanplex`, so commit or not,
what is deployed is what is on disk.

The front door (§5.9) runs beside it: `com.homeserv.cleanplex-frontdoor` (nginx on 32480) and the pf
anchor `com.apple/cleanplex` (loaded by `com.homeserv.cleanplex-pf`). Plex is `com.homeserv.plex`.

1. Confirm the dev instance passes every check in §10.2
2. Back up the database first — migrations run against it on startup:
   `sudo cp /Users/server/.cleanplex/cleanplex.db /Users/server/.cleanplex/cleanplex.db.bak-$(date +%Y%m%d-%H%M%S)`
3. Deploy and restart: `sudo bash scripts/install-macos-service.sh server` (waits for the old process to exit)
4. Verify: poll `http://localhost:7979/api/settings` until it responds
5. Confirm the process actually recycled — its PID (`pgrep -f bin/cleanplex`) must differ from before
6. Check `/Users/server/.cleanplex/cleanplex.log` for errors before declaring done
7. If the front door is installed, check Plex still answers through it:
   `curl -s http://192.168.0.80:32400/identity` and, after a front door change, a phone on Wi-Fi

Front door changes: `sudo bash scripts/frontdoor/install.sh` / `--uninstall` (§5.9). **Any change to the
redirect or nginx config must be proven from another device on a spare port before it touches 32400** —
this machine's own traffic takes a different (loopback) path and cannot show a redirect is broken.
Reporting a deploy as done without a changed PID is a false success.