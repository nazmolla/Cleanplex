"""Hayaa sync package (#222, #234): typed sync records, components and startup-owned service."""

from __future__ import annotations

from typing import Any

from .applier import ClaimApplier
from .content_keys import ContentKeyStore
from .models import (
    AUTO_BUDGET,
    AUTO_CHECK_S,
    BODY_BUDGET,
    DEFAULT_PAUSE_S,
    DISAGREE_MIN_SAMPLES,
    DISAGREE_SWITCH,
    FAMILY_CATEGORY,
    FAMILY_OF_SOURCE,
    MEDIA_PER_PUSH,
    OBS_PER_PUSH,
    PRESETS,
    PULL_BATCH,
    PULL_JOB_POLLS,
    PULLS_PER_DAY,
    PUSHES_PER_DAY,
    TOLERANCE_MS,
    ApplyCounts,
    HayaaClaim,
    HayaaTitleRecord,
    LedgerEntry,
    Observation,
    PullPending,
    PushPlan,
    SyncResult,
    SyncTitle,
    chunk_by_size,
    obs_id,
    overlaps,
    parse_view as _parse_view_claims,
)
from .planner import PushPlanner
from .puller import HayaaPuller
from .pusher import HayaaPusher
from .quota import QuotaTracker
from .service import HayaaSyncService
from .status import SyncStatusReader

__all__ = [
    "AUTO_BUDGET",
    "AUTO_CHECK_S",
    "BODY_BUDGET",
    "DEFAULT_PAUSE_S",
    "DISAGREE_MIN_SAMPLES",
    "DISAGREE_SWITCH",
    "FAMILY_CATEGORY",
    "FAMILY_OF_SOURCE",
    "MEDIA_PER_PUSH",
    "OBS_PER_PUSH",
    "PRESETS",
    "PULL_BATCH",
    "PULL_JOB_POLLS",
    "PULLS_PER_DAY",
    "PUSHES_PER_DAY",
    "TOLERANCE_MS",
    "ApplyCounts",
    "ClaimApplier",
    "ContentKeyStore",
    "HayaaClaim",
    "HayaaPuller",
    "HayaaPusher",
    "HayaaSyncService",
    "HayaaTitleRecord",
    "LedgerEntry",
    "Observation",
    "PullPending",
    "PushPlan",
    "PushPlanner",
    "QuotaTracker",
    "SyncResult",
    "SyncStatusReader",
    "SyncTitle",
    "apply_titles",
    "auto_sync_due",
    "decide",
    "desired_observations",
    "get_preset",
    "get_service",
    "hayaa_sync_loop",
    "init_service",
    "match_claim",
    "needs_verify",
    "obs_id",
    "on_false_positive_cleared",
    "on_segment_deleted",
    "overlaps",
    "parse_view",
    "plan_push",
    "review",
    "run_sync",
    "running",
    "start_manual_sync",
    "sync_status",
    "threshold_ok",
]

# Created once at application startup via init_service(); created lazily in tests that skip main().
_service: HayaaSyncService | None = None


def init_service() -> HayaaSyncService:
    """Create (or replace) the HayaaSyncService instance at application startup."""
    global _service
    _service = HayaaSyncService(poll_limit_fn=lambda: PULL_JOB_POLLS)
    return _service


def get_service() -> HayaaSyncService:
    """Return the active HayaaSyncService instance, creating one if not yet initialized."""
    global _service
    if _service is None:
        _service = HayaaSyncService(poll_limit_fn=lambda: PULL_JOB_POLLS)
    return _service


# ── Functional compatibility surface for routes, scanner, verify and tests ─────

def running() -> bool:
    return get_service().running()


async def run_sync(manual: bool, force: bool = False) -> dict[str, Any]:
    return await get_service().run_sync(manual=manual, force=force)


async def start_manual_sync(force: bool = False) -> dict[str, Any]:
    return await get_service().start_manual_sync(force=force)


async def sync_status() -> dict[str, Any]:
    return await get_service().sync_status()


async def auto_sync_due(now: float | None = None) -> bool:
    return await get_service().auto_sync_due(now=now)


async def hayaa_sync_loop() -> None:
    await get_service().sync_loop()


async def apply_titles(plex_guids: list[str], config: Any = None) -> dict[str, int]:
    return (await get_service().applier.apply_titles(plex_guids, config=config)).to_dict()


async def review(title_key: str, claim_id: str, keep: bool) -> dict[str, int]:
    return await get_service().applier.review(title_key, claim_id, keep)


async def on_segment_deleted(seg: dict[str, Any]) -> None:
    await get_service().applier.on_segment_deleted(seg)


async def on_false_positive_cleared(seg: dict[str, Any]) -> None:
    await get_service().applier.on_false_positive_cleared(seg)


async def get_preset() -> str:
    return await get_service().applier.get_preset()


async def _check_disagreement() -> None:
    await get_service().applier.check_disagreement()


def _utc_day() -> str:
    return QuotaTracker.utc_day()


def _by_size(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return chunk_by_size(items)


def match_claim(seg: dict[str, Any], claims: list[dict[str, Any] | HayaaClaim]) -> dict[str, Any] | None:
    typed = [c if isinstance(c, HayaaClaim) else HayaaClaim.from_row(c) for c in claims]
    found = get_service().planner.match_claim(seg, typed)
    if found is None:
        return None
    for raw, obj in zip(claims, typed):
        if obj is found:
            return raw if isinstance(raw, dict) else obj.to_db_dict()
    return None


def desired_observations(
    title_key: str,
    job: dict[str, Any] | SyncTitle,
    segments: list[dict[str, Any]],
    claims: list[dict[str, Any] | HayaaClaim],
    overrides: dict[tuple[str, str], str],
    nudenet_detector: str,
) -> dict[str, dict[str, Any]]:
    title = job if isinstance(job, SyncTitle) else SyncTitle.from_row(job)
    typed_claims = [c if isinstance(c, HayaaClaim) else HayaaClaim.from_row(c) for c in claims]
    want = get_service().planner.desired_observations(
        title_key, title, segments, typed_claims, overrides, nudenet_detector
    )
    return {base: {"title": obs.title_key, "kind": obs.kind, **obs.body} for base, obs in want.items()}


def plan_push(
    desired: dict[str, dict[str, Any] | Observation],
    ledger: list[dict[str, Any] | LedgerEntry],
    title_key: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str, str]], list[str]]:
    typed_desired: dict[str, Observation] = {}
    for base, item in desired.items():
        if isinstance(item, Observation):
            typed_desired[base] = item
        else:
            body = {k: v for k, v in item.items() if k not in ("title", "kind")}
            typed_desired[base] = Observation(base_id=base, title_key=item.get("title", title_key), kind=item["kind"], body=body)
    typed_ledger = [r if isinstance(r, LedgerEntry) else LedgerEntry.from_row(r) for r in ledger]
    plan = get_service().planner.plan_push(typed_desired, typed_ledger, title_key)
    return plan.observations, plan.ledger_rows, plan.retracted_ids


def parse_view(view: Any, runtime_ms: int | None) -> list[dict[str, Any]] | None:
    claims = _parse_view_claims(view, runtime_ms)
    return [c.to_db_dict() for c in claims] if claims is not None else None


def threshold_ok(claim: dict[str, Any] | HayaaClaim, config: Any) -> bool:
    typed = claim if isinstance(claim, HayaaClaim) else HayaaClaim.from_row(claim)
    return ClaimApplier.threshold_ok(typed, config)


def decide(claim: dict[str, Any] | HayaaClaim, override: str | None, preset: str, thresholds_pass: bool) -> str:
    typed = claim if isinstance(claim, HayaaClaim) else HayaaClaim.from_row(claim)
    return ClaimApplier.decide(typed, override, preset, thresholds_pass)


def needs_verify(claim: dict[str, Any] | HayaaClaim) -> bool:
    typed = claim if isinstance(claim, HayaaClaim) else HayaaClaim.from_row(claim)
    return ClaimApplier.needs_verify(typed)
