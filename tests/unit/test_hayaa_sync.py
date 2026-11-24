"""Hayaa sync engine (#222): observations, push diff, view parsing, apply matrix, full sync runs."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from cleanplex import database as db
from cleanplex import hayaa, hayaa_sync as hs

pytestmark = pytest.mark.usefixtures("setup_db")
TK = "a" * 64


@pytest.fixture(autouse=True)
def reset_state():
    hayaa._access, hayaa._link, hayaa._link_task, hayaa._http = None, None, None, None
    yield
    hayaa._access, hayaa._link, hayaa._link_task, hayaa._http = None, None, None, None


def _seg(**kw):
    base = {"id": 1, "plex_guid": "g1", "start_ms": 10_000, "end_ms": 20_000, "source": "scanner",
            "false_positive": 0, "labels": "FEMALE_BREAST_EXPOSED", "detected_labels": "FEMALE_BREAST_EXPOSED",
            "corrected_labels": "", "confidence": 0.8, "detector": "nudenet-320n", "severity": "medium",
            "hayaa_claim_id": ""}
    return {**base, **kw}


def _job(**kw):
    return {"plex_guid": "g1", "duration_ms": 6_000_000, "status": "done", "scan_version": 4,
            "finished_at": "2026-11-01 00:00:00", **kw}


def _claim(**kw):
    base = {"title_key": TK, "claim_id": "c1", "plex_guid": "g1", "start_ms": 10_000, "end_ms": 20_000,
            "status": "accepted", "basis": "machine", "labels": "FEMALE_BREAST_EXPOSED", "category": "nudity",
            "severity": "medium", "machine": json.dumps({"nudenet": 0.9}), "supporters": 2, "rejecters": 0,
            "verify_state": "", "verify_family": "", "verify_conf": None, "verify_fp_drop": 0, "verify_thumb": ""}
    return {**base, **kw}


# ── desired_observations ──────────────────────────────────────────────────────

def test_detect_carries_detector_labels_category_and_seconds():
    want = hs.desired_observations(TK, _job(), [_seg()], [], {}, "nudenet-320n")
    (obs,) = want.values()
    assert obs["kind"] == "detect" and obs["detector"] == "nudenet" and obs["category"] == "nudity"
    assert obs["start"] == 10.0 and obs["end"] == 20.0 and obs["labels"] == ["FEMALE_BREAST_EXPOSED"]
    assert obs["detector_version"] == "nudenet-320n" and obs["runtime"] == 6000.0


def test_imported_and_pulled_segments_are_never_sent_as_detections():
    segs = [_seg(source="skp"), _seg(id=2, source="edl"), _seg(id=3, source="hayaa")]
    want = hs.desired_observations(TK, _job(status="pending"), segs, [], {}, "nudenet-320n")
    assert want == {}


def test_user_false_positive_and_verifier_rejection_are_not_detections():
    segs = [_seg(false_positive=db.FP_MARKED), _seg(id=2, start_ms=30_000, end_ms=40_000, false_positive=db.FP_VERIFIER)]
    want = hs.desired_observations(TK, _job(status="pending"), segs, [], {}, "nudenet-320n")
    assert want == {}


def test_relabel_keeps_the_detector_labels_and_votes_the_correction_on_the_claim():
    seg = _seg(labels="BUTTOCKS_EXPOSED", detected_labels="FEMALE_BREAST_EXPOSED", corrected_labels="BUTTOCKS_EXPOSED")
    want = hs.desired_observations(TK, _job(), [seg], [_claim()], {}, "nudenet-320n")
    kinds = {o["kind"]: o for o in want.values()}
    assert kinds["detect"]["labels"] == ["FEMALE_BREAST_EXPOSED"]
    assert kinds["label"]["labels"] == ["BUTTOCKS_EXPOSED"] and kinds["label"]["claim_id"] == "c1"


def test_false_positive_on_a_claimed_segment_is_a_reject_vote_not_a_clean_scan():
    want = hs.desired_observations(TK, _job(), [_seg(false_positive=1)], [_claim()], {}, "nudenet-320n")
    assert [o["kind"] for o in want.values()] == ["reject"]


def test_subtitle_segments_send_severity_but_no_words():
    seg = _seg(source="subtitles", labels="damn", detected_labels="damn", severity="high", detector="")
    (obs,) = hs.desired_observations(TK, _job(status="pending"), [seg], [], {}, "nudenet-320n").values()
    assert obs["detector"] == "subtitles" and "labels" not in obs and obs["severity"] == "high"
    assert obs["category"] == "language"


def test_finished_scan_without_detections_is_clean():
    (obs,) = hs.desired_observations(TK, _job(), [], [], {}, "nudenet-320n").values()
    assert obs["kind"] == "clean" and obs["detector"] == "nudenet"


def test_siglip_confidence_drops_the_ranking_offset():
    seg = _seg(source="siglip", confidence=1.42, detector="siglip-base-224")
    (obs,) = hs.desired_observations(TK, _job(), [seg], [], {}, "nudenet-320n").values()
    assert obs["confidence"] == 0.42 and obs["detector"] == "siglip"


def test_kept_claim_is_a_confirm_and_a_verified_one_a_verify_hit():
    claims = [_claim(), _claim(claim_id="c2", start_ms=50_000, end_ms=60_000, verify_state="hit",
                               verify_family="nudenet", verify_conf=0.77)]
    want = hs.desired_observations(TK, _job(status="pending"), [], claims, {(TK, "c1"): "keep"}, "nudenet-320n")
    kinds = sorted(o["kind"] for o in want.values())
    assert kinds == ["confirm", "verify_hit"]


def test_range_past_the_runtime_is_left_out():
    want = hs.desired_observations(TK, _job(duration_ms=15_000), [_seg()], [], {}, "nudenet-320n")
    assert all(o["kind"] != "detect" for o in want.values())


# ── plan_push ─────────────────────────────────────────────────────────────────

def test_plan_push_sends_new_and_retracts_withdrawn():
    want = {"b1": {"title": TK, "kind": "detect"}}
    ledger = [{"obs_id": "b0", "base_id": "b0", "title_key": TK, "kind": "detect", "live": 1}]
    push, rows, retracted = hs.plan_push(want, ledger, TK)
    assert [o["id"] for o in push if o["kind"] == "detect"] == ["b1"]
    retract = [o for o in push if o["kind"] == "retract"]
    assert retract[0]["retracts"] == ["b0"] and retracted == ["b0"]


def test_plan_push_skips_what_is_already_live():
    ledger = [{"obs_id": "b1", "base_id": "b1", "title_key": TK, "kind": "detect", "live": 1}]
    assert hs.plan_push({"b1": {"title": TK, "kind": "detect"}}, ledger, TK) == ([], [], [])


def test_reasserting_a_retracted_observation_uses_a_new_id():
    ledger = [{"obs_id": "b1", "base_id": "b1", "title_key": TK, "kind": "detect", "live": 0}]
    push, _, _ = hs.plan_push({"b1": {"title": TK, "kind": "detect"}}, ledger, TK)
    assert push[0]["id"] == "b1-1"


# ── parse_view and decide ─────────────────────────────────────────────────────

def _view(**claim):
    c = {"claim_id": "c1", "start": 10.0, "end": 20.0, "status": "accepted", "basis": "machine",
         "labels": ["FEMALE_BREAST_EXPOSED"], "category": "nudity", "severity": "medium",
         "machine": {"nudenet": 0.9}, "supporters": 2, "rejecters": 0, **claim}
    return {"claims": [c], "version": "v1"}


def test_parse_view_converts_to_ms():
    (c,) = hs.parse_view(_view(), 6_000_000)
    assert c["start_ms"] == 10_000 and c["end_ms"] == 20_000 and json.loads(c["machine"]) == {"nudenet": 0.9}


@pytest.mark.parametrize("bad", [{"labels": ["NOT_A_LABEL"]}, {"status": "weird"}, {"end": 9999.0}, {"start": 30.0}])
def test_parse_view_drops_invalid_views(bad):
    assert hs.parse_view(_view(**bad), 6_000_000) is None


@pytest.mark.parametrize("claim,override,preset,expected", [
    (_claim(), None, "balanced", "apply"),
    (_claim(status="candidate"), None, "balanced", "verify"),
    (_claim(status="candidate", verify_state="hit"), None, "balanced", "apply"),
    (_claim(status="candidate", verify_state="miss"), None, "balanced", "hold"),
    (_claim(status="candidate"), None, "cautious", "apply"),
    (_claim(status="rejected"), None, "cautious", "skip"),
    (_claim(verify_state="miss", verify_family="nudenet"), None, "balanced", "hold"),
    (_claim(basis="both", verify_state="miss", verify_family="nudenet"), None, "balanced", "apply"),
    (_claim(status="disputed", verify_state="hit", verify_fp_drop=1), None, "balanced", "hold"),
    (_claim(status="disputed"), None, "balanced", "apply"),
    (_claim(), None, "verify_first", "verify"),
    (_claim(verify_state="hit"), None, "verify_first", "apply"),
    (_claim(status="rejected"), "keep", "balanced", "apply"),
    (_claim(), "delete", "balanced", "skip"),
])
def test_apply_matrix(claim, override, preset, expected):
    assert hs.decide(claim, override, preset, True) == expected


def test_machine_claim_below_local_threshold_is_skipped_but_human_one_applies():
    config = SimpleNamespace(threshold_for=lambda lbl: 0.95, siglip_threshold_for=lambda lbl: 0.5)
    assert not hs.threshold_ok(_claim(), config)
    assert hs.decide(_claim(), None, "balanced", hs.threshold_ok(_claim(), config)) == "skip"
    assert hs.threshold_ok(_claim(basis="human"), config)


# ── apply_titles against the database ─────────────────────────────────────────

async def _title(guid="g1"):
    await db.upsert_scan_jobs_batch([{"plex_guid": guid, "title": "Film", "file_path": "/m/film.mkv",
                                      "rating_key": "1", "library_id": "1", "library_title": "Movies"}])
    await db.upsert_hayaa_titles([(TK, guid)])


async def test_accepted_claim_becomes_a_pulled_segment_and_a_deleted_one_never_returns():
    await _title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(), None)[0]])
    counts = await hs.apply_titles(["g1"])
    assert counts["applied"] == 1
    (seg,) = await db.get_segments_for_guid("g1")
    assert seg["source"] == "hayaa" and seg["hayaa_claim_id"] == "c1"
    await hs.on_segment_deleted(seg)
    from cleanplex import segment_files
    await segment_files.delete_segment(seg["id"])
    await hs.apply_titles(["g1"])
    assert await db.get_segments_for_guid("g1") == []


async def test_local_detection_is_linked_not_duplicated():
    await _title()
    sid = await db.insert_segment("g1", "Film", 11_000, 19_000, labels="FEMALE_BREAST_EXPOSED", severity="medium")
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(), None)[0]])
    counts = await hs.apply_titles(["g1"])
    assert counts["applied"] == 0 and counts["linked"] == 1
    (seg,) = await db.get_segments_for_guid("g1")
    assert seg["id"] == sid and seg["hayaa_claim_id"] == "c1"


async def test_community_rejection_hides_a_local_medium_detection_until_restored():
    await _title()
    sid = await db.insert_segment("g1", "Film", 11_000, 19_000, labels="FEMALE_BREAST_EXPOSED", severity="medium")
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(status="rejected"), None)[0]])
    await hs.apply_titles(["g1"])
    seg = await db.get_segment_by_id(sid)
    assert seg["false_positive"] == db.FP_COMMUNITY
    await hs.on_false_positive_cleared(seg)
    await db.unmark_segment_false_positive(sid)
    await hs.apply_titles(["g1"])
    assert (await db.get_segment_by_id(sid))["false_positive"] == 0


async def test_community_rejection_keeps_a_high_severity_local_detection():
    await _title()
    sid = await db.insert_segment("g1", "Film", 11_000, 19_000, labels="FEMALE_GENITALIA_EXPOSED", severity="high")
    claim = hs.parse_view(_view(status="rejected", severity="high", labels=["FEMALE_GENITALIA_EXPOSED"]), None)[0]
    await db.replace_hayaa_claims(TK, "g1", [claim])
    await hs.apply_titles(["g1"])
    assert (await db.get_segment_by_id(sid))["false_positive"] == 0
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["decision"] == "ask"


async def test_candidate_waits_for_a_recheck_and_shows_in_review():
    await _title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(status="candidate"), None)[0]])
    await hs.apply_titles(["g1"])
    assert await db.get_segments_for_guid("g1") == []
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["verify_state"] == "pending" and c["decision"] == "verify"
    assert len(await db.get_hayaa_review()) == 1
    await hs.review(TK, "c1", keep=True)
    assert len(await db.get_segments_for_guid("g1")) == 1 and await db.get_hayaa_review() == []


async def test_claim_that_disappears_removes_its_pulled_segment():
    await _title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(), None)[0]])
    await hs.apply_titles(["g1"])
    await db.replace_hayaa_claims(TK, "g1", [])
    await hs.apply_titles(["g1"])
    assert await db.get_segments_for_guid("g1") == []


async def test_language_claims_are_not_rechecked_and_mute():
    await _title()
    claim = hs.parse_view(_view(labels=[], category="language", severity="high", machine={"subtitles": 1.0}), None)[0]
    await db.replace_hayaa_claims(TK, "g1", [claim])
    await hs.apply_titles(["g1"])
    (seg,) = await db.get_segments_for_guid("g1")
    assert seg["action"] == "mute" and seg["channel"] == "audio"
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["verify_state"] == "na"


async def test_purging_a_removed_title_drops_its_staged_claims():
    await _title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(), None)[0]])
    async with db.get_connection() as conn:
        await conn.execute("DELETE FROM scan_jobs WHERE plex_guid='g1'")
        await conn.execute("INSERT INTO removed_titles(plex_guid, removed_at) VALUES('g1', datetime('now','-60 days'))")
        await conn.commit()
    await db.purge_removed_titles(30)
    assert await db.get_hayaa_claims(["g1"]) == [] and await db.get_hayaa_titles() == {}


# ── Full sync against a fake Hayaa ────────────────────────────────────────────

class FakeApi:
    def __init__(self, views=None, busy=False, challenge=None, deferred=None):
        self.pushed, self.media, self.pulls, self.proofs = [], [], [], []
        self.views, self.busy = views or {}, busy
        self.challenge, self.deferred = challenge or {}, deferred or {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.busy:
            return httpx.Response(429, headers={"Retry-After": "600"})
        assert req.headers["x-hy-key"] and req.headers["x-hy-signature"]
        body = json.loads(req.content)
        if req.url.path == "/api/v1/push":
            self.pushed += body["observations"]
            if body.get("proofs"):
                self.proofs.append(body["proofs"])
                return httpx.Response(200, json={"accepted": 0, "challenges": {}})
            asked = {k: v for k, v in self.challenge.items()
                     if any(o["title"] == k for o in body["observations"])}
            return httpx.Response(200, json={"accepted": len(body["observations"]), "challenges": asked})
        if req.url.path == "/api/v1/media":
            self.media += body["media"]
            return httpx.Response(200, json={"stored": len(body["media"])})
        if req.url.path == "/api/v1/pull":
            self.pulls.append(body["have"])
            return httpx.Response(200, json={
                "views": {k: v for k, v in self.views.items() if k in body["have"]},
                "deferred": {k: v for k, v in self.deferred.items() if k in body["have"]},
                "pad": "   "})
        return httpx.Response(404, json={"error": "nope"})


async def _linked_title(tmp_path, fake: FakeApi):
    f = tmp_path / "film.mkv"
    f.write_bytes(b"x" * 5000)
    await db.upsert_scan_jobs_batch([{"plex_guid": "g1", "title": "Film", "file_path": str(f), "rating_key": "1",
                                      "library_id": "1", "library_title": "Movies"}])
    await db.set_setting("hayaa_api_url", "https://api.test")
    await db.set_setting("hayaa_auth_url", "https://auth.test")
    hayaa._write_private(hayaa._token_path(), b"ref")
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return await hayaa.content_key_for(str(f))


async def test_sync_describes_pushes_pulls_and_applies(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.views = {key: _view()}
    await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium",
                            detector="nudenet-320n", confidence=0.7)
    result = await hs.run_sync(manual=True)
    assert result["described"] == 1 and fake.media[0]["release_key"] == hayaa.release_key("film.mkv", 5000)
    assert [o["kind"] for o in fake.pushed] == ["detect"]
    assert result["changed"] == 1 and result["applied"] == 1
    assert {s["source"] for s in await db.get_segments_for_guid("g1")} == {"scanner", "hayaa"}

    # Nothing changed: the second sync sends nothing and asks with the version it holds.
    fake.pushed.clear()
    await hs.run_sync(manual=True)
    assert fake.pushed == [] and fake.pulls[-1] == {key: "v1"}


async def test_sync_answers_possession_challenges_from_the_file(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.challenge = {key: [0, 512]}
    await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    result = await hs.run_sync(manual=True)
    want = {"0": hayaa.proof_answer((b"x" * 5000)[:65536]), "512": hayaa.proof_answer(b"x" * 5000)}
    assert fake.proofs == [{key: want}] and result["proved"] == 1


async def test_sync_reports_titles_hayaa_holds_back(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.deferred = {key: {"reason": "budget", "retry_after": 3600}}
    result = await hs.run_sync(manual=True)
    assert result["deferred"] == {"budget": 1} and result["changed"] == 0


async def test_partly_released_titles_are_pulled_first_next_time(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.views = {key: {**_view(), "complete": False}}
    await hs.run_sync(manual=True)
    assert json.loads(await db.get_setting("hayaa_partial")) == [key]
    fake.views = {key: {**_view(), "version": "v2", "complete": True}}
    await hs.run_sync(manual=True)
    assert json.loads(await db.get_setting("hayaa_partial")) == []


async def test_deleting_own_segment_retracts_it_on_the_next_sync(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    sid = await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    await hs.run_sync(manual=True)
    detect_id = fake.pushed[0]["id"]
    from cleanplex import segment_files
    await segment_files.delete_segment(sid)
    await hs.run_sync(manual=True)
    assert fake.pushed[-1]["kind"] == "retract" and fake.pushed[-1]["retracts"] == [detect_id]


async def test_busy_service_pauses_sync_until_retry_after(tmp_path):
    fake = FakeApi(busy=True)
    await _linked_title(tmp_path, fake)
    with pytest.raises(hayaa.HayaaBusy):
        await hs.run_sync(manual=True)
    paused = float(await db.get_setting("hayaa_paused_until"))
    assert paused > time.time() + 500
    with pytest.raises(hayaa.HayaaBusy):
        await hs.run_sync(manual=True)  # refused locally, no request made
    assert not await hs.auto_sync_due()


async def test_automatic_sync_stays_inside_its_share_of_the_quota(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    await db.set_setting("hayaa_quota", json.dumps({"day": hs._utc_day(), "pushes": 16, "pulls": 48}))
    result = await hs.run_sync(manual=False)
    assert result["skipped_quota"] and fake.media == [] and fake.pulls == []


async def test_auto_sync_due_needs_opt_in_link_and_interval(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    assert not await hs.auto_sync_due()
    await db.set_setting("hayaa_auto_sync", "1")
    assert await hs.auto_sync_due()
    await db.set_setting("hayaa_last_sync", str(time.time()))
    assert not await hs.auto_sync_due()
    assert await hs.auto_sync_due(now=time.time() + 13 * 3600)


async def test_local_rechecks_that_disagree_switch_to_verify_first():
    await _title()
    claims = [hs.parse_view(_view(claim_id=f"c{i}", start=10.0 + i * 30, end=20.0 + i * 30), None)[0] for i in range(20)]
    await db.replace_hayaa_claims(TK, "g1", claims)
    for i in range(3):
        await db.update_hayaa_claim(TK, f"c{i}", verify_state="miss", verify_family="nudenet")
    for i in range(3, 20):
        await db.update_hayaa_claim(TK, f"c{i}", verify_state="hit", verify_family="nudenet")
    await hs._check_disagreement()
    assert await hs.get_preset() == "verify_first"
