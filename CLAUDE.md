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
| Video | FFmpeg / ffprobe | Subprocess via `frame_extractor.py` |
| Plex API | `plexapi` + `httpx` | Wrapped in `PlexClient` |
| Frontend | React, TypeScript, Vite, Tailwind | SPA served from `frontend/` |
| Tests | `pytest`, `pytest-asyncio` | See §6 |

**Module ownership:**

| Module | Responsibility |
|---|---|
| `database.py` | All SQL — no raw queries outside this file |
| `scanner.py` | Frame extraction, NudeNet inference, segment writing |
| `plex_client.py` | All Plex API calls — no `plexapi` imports elsewhere |
| `filter_engine.py` | Playback position checks and seek decisions |
| `watcher.py` | Polling loops only — no business logic |
| `sync.py` / `sync_merge.py` | GitHub sync and segment merge logic |
| `bg_jobs.py` | Background job lifecycle — no domain logic |
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

### 5.4 Scanner

- **One NudeNet detector per thread.** Use `threading.local()` — never instantiate inside a tight frame loop.
- **Scanner global state (`_queued_guids`, `_current_scan_guids`, `_paused`) is guarded by a lock** before mutation.
- **Segments are clustered before DB insert.** Never insert a raw frame-level row; always cluster via `_cluster_frames` / `_flush_cluster`.

### 5.5 Frontend

- **All API calls go through `src/api/`.** No `fetch`/`axios` calls inline in components or pages.
- **Polling intervals must cancel in-flight requests.** Use `AbortController` before each new poll tick.
- **No polling loop may run unbounded.** Every `setInterval`/recursive `setTimeout` must have a terminal condition (success, error, or max-duration).
- **Bulk actions must use bounded concurrency or a batch endpoint.** Never fire N sequential requests from a UI loop.

### 5.6 Sync

- **Sync is always manually triggered.** No background task or watcher may initiate a sync operation automatically.
- **File hash cache is keyed by `(path, size, mtime)`.** Recompute SHA256 only when any of these change.
- **Segment merge complexity must be sub-quadratic on practical inputs.** Use time-bucketed or sorted interval matching.

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

`dev-start.sh --fresh` wipes `~/.cleanplex-dev/` and reseeds from production.

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

Never kill production before the replacement is confirmed healthy:

1. Start replacement on a different port OR confirm the dev instance passes all checks
2. Kill production: `powershell -Command "Get-Process cleanplex -ErrorAction SilentlyContinue | Stop-Process -Force"`
3. Start production: `.venv/Scripts/cleanplex.exe > cleanplex_restart.log 2>&1 &`
4. Verify: poll `http://localhost:7979/api/settings` until it responds
5. Check log for errors before declaring done
