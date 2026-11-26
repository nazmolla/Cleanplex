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


async def test_label_less_claim_applies_and_takes_labels_from_the_local_recheck():
    """Hayaa strips labels (ABUSE_RESISTANCE §7). The pulled segment must still apply, and get the
    labels this server's own re-check finds, without the user's own relabel being overwritten."""
    await _title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(labels=[]), None)[0]])
    assert (await hs.apply_titles(["g1"]))["applied"] == 1
    (seg,) = await db.get_segments_for_guid("g1")
    assert seg["labels"] == "" and seg["false_positive"] == 0
    await db.update_hayaa_claim(TK, "c1", verify_state="hit", verify_family="nudenet",
                                verify_labels="BUTTOCKS_EXPOSED")
    await hs.apply_titles(["g1"])
    (seg,) = await db.get_segments_for_guid("g1")
    assert seg["labels"] == "BUTTOCKS_EXPOSED" and seg["false_positive"] == 0
    await db.update_hayaa_claim(TK, "c1", verify_labels="FEMALE_GENITALIA_EXPOSED")
    await hs.apply_titles(["g1"])
    assert (await db.get_segments_for_guid("g1"))[0]["labels"] == "BUTTOCKS_EXPOSED"


async def test_fill_hayaa_segment_labels_only_fills_empty_pulled_segments():
    await _title()
    own = await db.insert_segment("g1", "Film", 1000, 2000, labels="")
    assert not await db.fill_hayaa_segment_labels(own, "BUTTOCKS_EXPOSED")  # not a pulled segment
    assert not await db.fill_hayaa_segment_labels(own, "")


def test_threshold_check_uses_recheck_labels_when_hayaa_sends_none():
    cfg = SimpleNamespace(threshold_for=lambda lbl: 0.95 if lbl == "BUTTOCKS_EXPOSED" else 0.5,
                          siglip_threshold_for=lambda lbl: 0.5)
    claim = {"basis": "machine", "machine": json.dumps({"nudenet": 0.7}), "labels": ""}
    assert hs.threshold_ok(claim, cfg)  # no labels anywhere: the global threshold
    assert not hs.threshold_ok({**claim, "verify_labels": "BUTTOCKS_EXPOSED"}, cfg)


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
        self.feed: list[str] = []  # titles the change feed lists after cursor "c0"
        self.cursors: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.busy:
            return httpx.Response(429, headers={"Retry-After": "600"})
        assert req.headers["x-hy-key"] and req.headers["x-hy-signature"]
        if req.method == "GET" and req.url.path == "/api/v1/changes":
            since = req.url.params.get("since", "")
            self.cursors.append(since)
            return httpx.Response(200, json={"titles": self.feed if since else [], "cursor": "c1",
                                             "more": False})
        body = json.loads(req.content)
        if req.url.path == "/api/v1/push":
            if body.get("proofs"):
                self.proofs.append(body["proofs"])
                return httpx.Response(200, json={"accepted": 0, "challenges": {}})
            self.pushed += body["observations"]
            return httpx.Response(202, json={"batch": "b1", "queued": len(body["observations"])})
        if req.url.path == "/api/v1/media":
            self.media += body["media"]
            return httpx.Response(202, json={"batch": "b2", "queued": len(body["media"])})
        if req.url.path == "/api/v1/pull":
            self.pulls.append(body["have"])
            return httpx.Response(200, json={
                "views": {k: v for k, v in self.views.items() if k in body["have"]},
                "deferred": {k: v for k, v in self.deferred.items() if k in body["have"]},
                "challenges": {k: v for k, v in self.challenge.items() if k in body["have"]},
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

    # Nothing changed: the second sync sends nothing and pulls nothing (the feed is empty)...
    fake.pushed.clear()
    pulls = len(fake.pulls)
    await hs.run_sync(manual=True)
    assert fake.pushed == [] and len(fake.pulls) == pulls and fake.cursors[-1] == "c1"
    # ...and when Hayaa's feed lists the title, it asks with the version it holds.
    fake.feed = [key]
    await hs.run_sync(manual=True)
    assert fake.pulls[-1] == {key: "v1"}


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
    # Describing files is not a push, so it still happens; pushes and pulls wait.
    assert result["skipped_quota"] and len(fake.media) == 1 and fake.pulls == []


async def test_manual_sync_asks_hayaa_whatever_the_local_count(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    await db.set_setting("hayaa_quota", json.dumps({"day": hs._utc_day(), "pushes": 99, "pulls": 99}))
    result = await hs.run_sync(manual=True)
    assert not result["skipped_quota"] and fake.pushed and fake.pulls


async def test_a_daily_quota_refusal_says_so_and_when_it_resumes(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        429, headers={"Retry-After": "600"}, json={"error": "daily media quota"})))
    with pytest.raises(hayaa.HayaaBusy):
        await hs.run_sync(manual=True)
    last = json.loads(await db.get_setting("hayaa_last_result", "{}"))
    assert last["error"].startswith("Daily upload limit reached; continues after")


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


# ── Removed titles are pushed upload-only (#228) ──────────────────────────────

async def _scanned_then_deleted(tmp_path, fake: FakeApi, *, purge: bool):
    """A title scanned (key cached during the scan), then deleted from disk and dropped by Plex."""
    key = await _linked_title(tmp_path, fake)
    await db.update_scan_job_status("g1", "done", progress=1.0, scan_version=3)
    (tmp_path / "film.mkv").unlink()
    assert await db.remove_titles_not_in("1", ["other"]) == 1
    if purge:
        async with db.get_connection() as conn:
            await conn.execute("UPDATE removed_titles SET removed_at = datetime('now','-60 days')")
            await conn.commit()
        await db.purge_removed_titles(30)
    return key


@pytest.mark.parametrize("purge", [False, True])
async def test_removed_title_detections_are_pushed_but_never_pulled_or_proved(tmp_path, purge):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium",
                            detector="nudenet-320n", confidence=0.7)
    key = await _scanned_then_deleted(tmp_path, fake, purge=purge)
    fake.challenge = {key: [0]}
    result = await hs.run_sync(manual=True)
    assert [(o["kind"], o["title"]) for o in fake.pushed] == [("detect", key)]
    assert fake.media[0]["release_key"] == hayaa.release_key("film.mkv", 5000)
    assert fake.pulls == [] and fake.proofs == [] and result["proved"] == 0

    fake.pushed.clear()  # sent once: the next sync has nothing new for it
    await hs.run_sync(manual=True)
    assert fake.pushed == []


async def test_removed_clean_scan_is_pushed_as_clean(tmp_path):
    fake = FakeApi()
    key = await _scanned_then_deleted(tmp_path, fake, purge=True)
    await hs.run_sync(manual=True)
    assert [(o["kind"], o["title"]) for o in fake.pushed] == [("clean", key)]


async def test_purge_keeps_an_unsent_content_key_and_drops_a_sent_one(tmp_path):
    fake = FakeApi()
    await _scanned_then_deleted(tmp_path, fake, purge=True)
    assert [t["plex_guid"] for t in await db.get_hayaa_removed_titles()] == ["g1"]

    await hs.run_sync(manual=True)  # sends the clean result
    async with db.get_connection() as conn:
        await conn.execute("UPDATE removed_titles SET purged_at = NULL")
        await conn.commit()
    await db.purge_removed_titles(30)
    assert await db.get_hayaa_removed_titles() == []


async def test_a_new_file_at_a_removed_path_is_not_treated_as_removed(tmp_path):
    fake = FakeApi()
    await _scanned_then_deleted(tmp_path, fake, purge=False)
    await db.upsert_scan_jobs_batch([{"plex_guid": "g2", "title": "Film", "file_path": str(tmp_path / "film.mkv"),
                                      "rating_key": "2", "library_id": "1", "library_title": "Movies"}])
    assert await db.get_hayaa_removed_titles() == []


def test_batches_stay_under_hayaas_body_limit():
    items = [{"title": "a" * 64, "release_key": "b" * 64, "runtime": 6000.0} for _ in range(3000)]
    parts = hs._by_size(items)
    assert sum(len(p) for p in parts) == 3000 and len(parts) > 1
    assert all(len(json.dumps({"media": p})) < 256 * 1024 for p in parts)


# ── Per-title sync state and content key (#231) ───────────────────────────────

async def _state(guid="g1"):
    async with db.get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT hayaa_sync_state, content_key FROM title_versions WHERE plex_guid=?", (guid,)
        )
    return dict(rows[0])


async def test_sync_sends_pending_titles_only_and_changes_make_them_pending(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    sid = await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    await hs.run_sync(manual=True)
    assert (await _state())["hayaa_sync_state"] == "synced" and len(fake.pushed) == 1

    fake.pushed.clear()
    await hs.run_sync(manual=True)
    assert fake.pushed == []  # synced: not even planned

    async with db.get_connection() as conn:  # the user marks the detection a false positive
        await conn.execute("UPDATE segments SET false_positive=1 WHERE id=?", (sid,))
        await conn.commit()
    assert (await _state())["hayaa_sync_state"] == "pending"
    await hs.run_sync(manual=True)
    assert fake.pushed and (await _state())["hayaa_sync_state"] == "synced"
    assert key


async def test_a_finished_scan_makes_the_title_pending(tmp_path):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    await hs.run_sync(manual=True)
    assert (await _state())["hayaa_sync_state"] == "synced"
    await db.update_scan_job_status("g1", "done", progress=1.0, scan_version=3)
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_sync_does_not_touch_the_file_once_the_key_is_stored(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    await hs.run_sync(manual=True)
    assert (await _state())["content_key"] == key
    (tmp_path / "film.mkv").unlink()  # the share is offline: sync still knows the title
    await db.insert_segment("g1", "Film", 200_000, 210_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    fake.pushed.clear()
    await hs.run_sync(manual=True)
    assert [o["title"] for o in fake.pushed] == [key]


async def test_force_sync_sends_and_pulls_everything(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    await db.insert_segment("g1", "Film", 100_000, 110_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    await hs.run_sync(manual=True)
    fake.pushed.clear()
    pulls = len(fake.pulls)
    await db.set_setting("hayaa_feed_cursor", "c1")
    await hs.run_sync(manual=True, force=True)
    assert fake.cursors[-1] == "" and len(fake.pulls) == pulls + 1 and key in fake.pulls[-1]
    assert fake.pushed == []  # already sent ids are not re-sent; the ledger knows them


async def test_a_large_pull_is_collected_from_its_job(tmp_path, monkeypatch):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.views = {key: _view()}
    base = fake.handler
    polls = []

    def handler(req):
        if req.url.path == "/api/v1/pull":
            json.loads(req.content)
            return httpx.Response(202, json={"job": "j1", "retry_after": 0})
        if req.url.path == "/api/v1/pull/result":
            polls.append(req.url.params["job"])
            if len(polls) < 2:
                return httpx.Response(202, json={"job": "j1", "retry_after": 0})
            return httpx.Response(200, json={"views": {key: _view()}, "deferred": {}, "challenges": {}})
        return base(req)
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await hs.run_sync(manual=True)
    assert polls == ["j1", "j1"] and result["changed"] == 1


async def test_a_pull_job_not_ready_in_time_waits_for_the_next_sync(tmp_path, monkeypatch):
    fake = FakeApi()
    await _linked_title(tmp_path, fake)
    monkeypatch.setattr(hs, "PULL_JOB_POLLS", 2)
    base = fake.handler

    def handler(req):
        if req.url.path in ("/api/v1/pull", "/api/v1/pull/result"):
            return httpx.Response(202, json={"job": "j1", "retry_after": 0})
        return base(req)
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await hs.run_sync(manual=True)
    assert result["pull_pending"] == 1 and not result["error"]


async def test_titles_waiting_for_another_holder_are_not_pulled_every_sync(tmp_path):
    fake = FakeApi()
    key = await _linked_title(tmp_path, fake)
    fake.deferred = {key: {"reason": "prove_possession", "retry_after": None}}
    await hs.run_sync(manual=True)
    pulls = len(fake.pulls)
    await hs.run_sync(manual=True)
    assert len(fake.pulls) == pulls  # not retried on its own...
    fake.feed = [key]
    await hs.run_sync(manual=True)
    assert len(fake.pulls) == pulls + 1  # ...only when the feed says the title changed


# ── SQLite triggers & typed package (#234) ────────────────────────────────────

async def _synced_title(guid="g1", content_key=TK):
    await db.upsert_scan_jobs_batch([{"plex_guid": guid, "title": "Film", "file_path": f"/m/{guid}.mkv",
                                      "rating_key": "1", "library_id": "1", "library_title": "Movies"}])
    await db.set_title_content_key(guid, content_key, 1000, 100)
    await db.mark_hayaa_sending([guid])
    await db.mark_hayaa_synced([guid])
    assert (await _state(guid))["hayaa_sync_state"] == "synced"


async def test_trigger_scan_done_marks_pending_but_progress_does_not():
    await _synced_title()
    await db.update_scan_job_status("g1", "scanning", progress=0.5)
    assert (await _state())["hayaa_sync_state"] == "synced"
    await db.update_scan_job_status("g1", "done", progress=1.0, scan_version=4)
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_trigger_segment_insert_marks_pending_only_for_own_segments():
    await _synced_title()
    await db.insert_segment("g1", "Film", 10_000, 20_000, source="hayaa", detector="hayaa")
    assert (await _state())["hayaa_sync_state"] == "synced"

    await db.insert_segment("g1", "Film", 30_000, 40_000, source="scanner", labels="BUTTOCKS_EXPOSED")
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_trigger_segment_update_marks_pending_for_own_and_fp_not_label_backfill():
    await _synced_title()
    own_id = await db.insert_segment("g1", "Film", 10_000, 20_000, source="scanner", labels="BUTTOCKS_EXPOSED")
    pulled_id = await db.insert_segment("g1", "Film", 30_000, 40_000, source="hayaa", labels="")
    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])

    # Backfilling labels on a pulled segment must not trigger a push.
    assert await db.fill_hayaa_segment_labels(pulled_id, "BUTTOCKS_EXPOSED")
    assert (await _state())["hayaa_sync_state"] == "synced"

    # Marking a pulled segment false positive IS a reject vote and must trigger a push.
    async with db.get_connection() as conn:
        await conn.execute("UPDATE segments SET false_positive=1 WHERE id=?", (pulled_id,))
        await conn.commit()
    assert (await _state())["hayaa_sync_state"] == "pending"

    # Editing an own segment's bounds/labels/false_positive triggers a push.
    for sql, params in [
        ("UPDATE segments SET labels='FEMALE_BREAST_EXPOSED' WHERE id=?", (own_id,)),
        ("UPDATE segments SET start_ms=11000 WHERE id=?", (own_id,)),
        ("UPDATE segments SET end_ms=21000 WHERE id=?", (own_id,)),
        ("UPDATE segments SET false_positive=1 WHERE id=?", (own_id,)),
    ]:
        await db.mark_hayaa_sending(["g1"])
        await db.mark_hayaa_synced(["g1"])
        async with db.get_connection() as conn:
            await conn.execute(sql, params)
            await conn.commit()
        assert (await _state())["hayaa_sync_state"] == "pending"


async def test_trigger_segment_delete_marks_pending_only_for_own_segments():
    await _synced_title()
    own_id = await db.insert_segment("g1", "Film", 10_000, 20_000, source="scanner", labels="BUTTOCKS_EXPOSED")
    pulled_id = await db.insert_segment("g1", "Film", 30_000, 40_000, source="hayaa")
    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])

    await db.delete_segment(pulled_id)
    assert (await _state())["hayaa_sync_state"] == "synced"

    await db.delete_segment(own_id)
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_trigger_feedback_marks_pending():
    await _synced_title()
    sid = await db.insert_segment("g1", "Film", 10_000, 20_000, source="scanner", labels="BUTTOCKS_EXPOSED")
    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])

    await db.mark_segment_false_positive(sid)
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_triggers_override_insert_update_delete_mark_pending():
    await _synced_title()
    await db.set_hayaa_override(TK, "c1", "keep")
    assert (await _state())["hayaa_sync_state"] == "pending"

    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])
    await db.set_hayaa_override(TK, "c1", "restore")
    assert (await _state())["hayaa_sync_state"] == "pending"

    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])
    await db.set_hayaa_override(TK, "c1", None)
    assert (await _state())["hayaa_sync_state"] == "pending"


async def test_trigger_verify_state_update_marks_pending():
    await _synced_title()
    await db.replace_hayaa_claims(TK, "g1", [hs.parse_view(_view(), None)[0]])
    await db.mark_hayaa_sending(["g1"])
    await db.mark_hayaa_synced(["g1"])

    await db.update_hayaa_claim(TK, "c1", verify_state="hit", verify_family="nudenet", verify_conf=0.8)
    assert (await _state())["hayaa_sync_state"] == "pending"


def test_hayaa_sync_service_instances_do_not_share_state():
    svc1 = hs.HayaaSyncService()
    svc2 = hs.HayaaSyncService()
    svc1._phase = "pushing"
    assert svc2.phase == ""
    assert not svc1.running() and not svc2.running()
