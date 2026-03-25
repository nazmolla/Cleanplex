"""Unit tests for sync_merge.py — SegmentMerger and resolve_segments."""

from __future__ import annotations

import pytest

from cleanplex.sync_merge import SegmentMerger, resolve_segments


# ── Helpers ────────────────────────────────────────────────────────────────────

def seg(start: int, end: int, confidence: float = 0.9, labels: str = "NUDITY") -> dict:
    return {"start_ms": start, "end_ms": end, "confidence": confidence, "labels": labels}


def cloud_source(segments: list[dict], instance: str = "cloud-1", level: str = "unverified") -> dict:
    return {"segments": segments, "source_instance": instance, "confidence_level": level}


# ── merge(): edge cases ────────────────────────────────────────────────────────

def test_merge_empty_returns_empty():
    merger = SegmentMerger(local_segments=[], cloud_sources=[])
    result, stats = merger.merge()
    assert result == []
    assert stats["status"] == "no_data"


def test_merge_local_only_returns_local():
    local = [seg(0, 1000), seg(5000, 6000)]
    merger = SegmentMerger(local_segments=local, cloud_sources=[])
    result, stats = merger.merge()
    assert result == local
    assert stats["status"] == "local_only"
    assert stats["merged_count"] == 2


def test_merge_cloud_only_returns_one_segment():
    cloud = [cloud_source([seg(0, 1000)])]
    merger = SegmentMerger(local_segments=[], cloud_sources=cloud)
    result, stats = merger.merge()
    assert len(result) == 1
    assert stats["status"] == "merged"


# ── _segments_match ────────────────────────────────────────────────────────────

def test_segments_match_within_tolerance():
    merger = SegmentMerger([], [], timing_tolerance_ms=2000)
    s1 = seg(10000, 20000)
    s2 = seg(10500, 19800)  # 500ms start diff, 200ms end diff — within 2000ms
    assert merger._segments_match(s1, s2) is True


def test_segments_match_outside_tolerance():
    merger = SegmentMerger([], [], timing_tolerance_ms=2000)
    s1 = seg(10000, 20000)
    s2 = seg(13000, 20000)  # 3000ms start diff — outside tolerance
    assert merger._segments_match(s1, s2) is False


def test_segments_match_exact():
    merger = SegmentMerger([], [], timing_tolerance_ms=2000)
    s1 = seg(5000, 10000)
    assert merger._segments_match(s1, s1) is True


def test_segments_match_end_outside_tolerance():
    merger = SegmentMerger([], [], timing_tolerance_ms=500)
    s1 = seg(0, 5000)
    s2 = seg(0, 6000)  # start matches, end diff = 1000ms > 500ms
    assert merger._segments_match(s1, s2) is False


# ── Clustering ─────────────────────────────────────────────────────────────────

def test_merge_clusters_matching_segments_from_two_sources():
    local = [seg(0, 1000)]
    cloud = [cloud_source([seg(100, 1100)], instance="cloud-1")]  # within 2000ms tolerance
    merger = SegmentMerger(local_segments=local, cloud_sources=cloud, timing_tolerance_ms=2000)
    result, stats = merger.merge()
    assert len(result) == 1
    assert stats["merged_count"] == 1
    # Two sources → sources_count=2
    assert result[0]["sources_count"] == 2


def test_merge_keeps_distinct_non_overlapping_segments():
    local = [seg(0, 1000)]
    cloud = [cloud_source([seg(50000, 60000)], instance="cloud-1")]  # far apart
    merger = SegmentMerger(local_segments=local, cloud_sources=cloud, timing_tolerance_ms=2000)
    result, _ = merger.merge()
    assert len(result) == 2


def test_merge_three_sources_cluster_together():
    local = [seg(1000, 2000, confidence=0.9)]
    cloud_1 = cloud_source([seg(1100, 2100)], instance="c1")
    cloud_2 = cloud_source([seg(900, 1900)], instance="c2")
    merger = SegmentMerger(local_segments=local, cloud_sources=[cloud_1, cloud_2], timing_tolerance_ms=2000)
    result, stats = merger.merge()
    assert len(result) == 1
    assert result[0]["sources_count"] == 3


def test_merge_result_sorted_by_start_ms():
    local = [seg(10000, 11000), seg(1000, 2000), seg(5000, 6000)]
    merger = SegmentMerger(local_segments=local, cloud_sources=[cloud_source([seg(8000, 9000)])])
    result, _ = merger.merge()
    starts = [r["start_ms"] for r in result]
    assert starts == sorted(starts)


# ── Confidence and source preference ──────────────────────────────────────────

def test_prefer_local_true_assigns_local_confidence_level():
    local = [seg(0, 1000)]
    cloud = [cloud_source([seg(100, 1000)], instance="c")]
    merger = SegmentMerger(local_segments=local, cloud_sources=cloud, prefer_local=True)
    result, _ = merger.merge()
    assert result[0]["confidence_level"] == "local"


def test_prefer_local_false_with_enough_sources_assigns_verified():
    # prefer_local=False and 2 cloud sources → verified
    cloud1 = cloud_source([seg(0, 1000)], instance="c1")
    cloud2 = cloud_source([seg(100, 900)], instance="c2")
    merger = SegmentMerger(
        local_segments=[],
        cloud_sources=[cloud1, cloud2],
        prefer_local=False,
        verified_threshold=2,
    )
    result, _ = merger.merge()
    assert result[0]["confidence_level"] == "verified"


def test_single_unverified_source_confidence_level():
    cloud = [cloud_source([seg(0, 1000)], instance="only")]
    merger = SegmentMerger(local_segments=[], cloud_sources=cloud, prefer_local=False)
    result, _ = merger.merge()
    assert result[0]["confidence_level"] == "unverified"


def test_confidence_score_weighted_by_source_type():
    merger = SegmentMerger([], [])
    s = seg(0, 1000, confidence=1.0)
    assert merger._calculate_confidence_score(s, "local") == pytest.approx(1.0)
    assert merger._calculate_confidence_score(s, "verified") == pytest.approx(0.85)
    assert merger._calculate_confidence_score(s, "unverified") == pytest.approx(0.6)


# ── Average timing ─────────────────────────────────────────────────────────────

def test_merged_segment_uses_average_timing():
    local = [seg(1000, 2000)]
    cloud = [cloud_source([seg(1500, 2500)], instance="c")]
    merger = SegmentMerger(local_segments=local, cloud_sources=cloud, timing_tolerance_ms=2000)
    result, _ = merger.merge()
    # Average: start=(1000+1500)/2=1250, end=(2000+2500)/2=2250
    assert result[0]["start_ms"] == 1250
    assert result[0]["end_ms"] == 2250


# ── Labels ─────────────────────────────────────────────────────────────────────

def test_most_common_labels_selected():
    local = [seg(0, 1000, labels="A")]
    cloud1 = cloud_source([seg(100, 900, labels="A")], instance="c1")
    cloud2 = cloud_source([seg(50, 950, labels="B")], instance="c2")
    merger = SegmentMerger(local_segments=local, cloud_sources=[cloud1, cloud2], timing_tolerance_ms=2000)
    result, _ = merger.merge()
    # "A" appears twice vs "B" once
    assert result[0]["labels"] == "A"


# ── resolve_segments (async wrapper) ──────────────────────────────────────────

async def test_resolve_segments_async_wrapper():
    local = [seg(0, 1000)]
    result, stats = await resolve_segments(
        file_hash="abc",
        local_segments=local,
        cloud_sources=[],
    )
    assert result == local
    assert stats["status"] == "local_only"


# ── Sort-then-sweep clustering correctness ─────────────────────────────────────

def test_cluster_sweep_does_not_merge_far_apart_segments():
    """Ensure the early-break in sweep doesn't accidentally skip valid comparisons."""
    local = [seg(0, 1000), seg(10000, 11000)]
    cloud = [cloud_source([seg(0, 1000), seg(10000, 11000)], instance="c")]
    merger = SegmentMerger(local_segments=local, cloud_sources=cloud, timing_tolerance_ms=2000)
    result, _ = merger.merge()
    # Two distinct groups of 2 — should produce 2 merged segments
    assert len(result) == 2
    for r in result:
        assert r["sources_count"] == 2


def test_cluster_sweep_large_input_all_distinct():
    """Verify O(n log n) sweep handles 100 non-overlapping segments correctly."""
    local = [seg(i * 10000, i * 10000 + 1000) for i in range(100)]
    merger = SegmentMerger(local_segments=local, cloud_sources=[])
    result, stats = merger.merge()
    assert stats["status"] == "local_only"
    assert len(result) == 100
