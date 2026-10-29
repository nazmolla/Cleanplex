"""Playlist editing and time mapping for the stream proxy prototype (#132)."""

from __future__ import annotations

import pytest

from cleanplex.stream_proxy import (
    edit_playlist, edited_to_original, original_to_edited, parse_cuts,
)


def _playlist(n: int, seg: int = 5, last: int | None = None) -> str:
    lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{seg}", "#EXT-X-ALLOW-CACHE:NO", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(n):
        dur = last if (last is not None and i == n - 1) else seg
        lines += [f"#EXTINF:{dur}, nodesc", f"{i:05d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _names(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.endswith(".ts")]


def test_parse_cuts_reads_comma_separated_ranges():
    assert parse_cuts("60000-75000, 300000-310000,") == [(60000, 75000), (300000, 310000)]
    assert parse_cuts("") == []


def test_no_ranges_leaves_the_playlist_unchanged():
    edit = edit_playlist(_playlist(4), [])
    assert edit.text == _playlist(4)
    assert (edit.removed, edit.spans, edit.kept) == ([], [], 4)


def test_segments_overlapping_a_range_are_dropped_and_the_cut_widens_to_boundaries():
    # 7s-12s overlaps segments 1 (5-10) and 2 (10-15); each is removed whole.
    edit = edit_playlist(_playlist(6), [(7_000, 12_000)])
    assert edit.removed == [1, 2]
    assert edit.spans == [(5_000, 15_000)]
    assert _names(edit.text) == ["00000.ts", "00003.ts", "00004.ts", "00005.ts"]


def test_a_range_ending_exactly_on_a_boundary_does_not_remove_the_next_segment():
    edit = edit_playlist(_playlist(4), [(5_000, 10_000)])
    assert edit.removed == [1]
    assert _names(edit.text) == ["00000.ts", "00002.ts", "00003.ts"]


def test_discontinuity_marks_each_gap_but_not_a_gap_at_the_start():
    edit = edit_playlist(_playlist(8), [(5_000, 10_000), (20_000, 25_000)])
    lines = edit.text.splitlines()
    assert lines.count("#EXT-X-DISCONTINUITY") == 2
    assert lines[lines.index("00002.ts") - 2] == "#EXT-X-DISCONTINUITY"

    at_start = edit_playlist(_playlist(4), [(0, 5_000)])
    assert "#EXT-X-DISCONTINUITY" not in at_start.text


def test_adjacent_removed_segments_merge_into_one_span_and_one_discontinuity():
    edit = edit_playlist(_playlist(6), [(5_000, 20_000)])
    assert edit.spans == [(5_000, 20_000)]
    assert edit.text.count("#EXT-X-DISCONTINUITY") == 1


def test_header_and_endlist_survive_and_a_short_last_segment_is_timed_correctly():
    edit = edit_playlist(_playlist(3, seg=5, last=2), [(10_000, 11_000)])
    assert edit.removed == [2]
    assert edit.text.startswith("#EXTM3U\n#EXT-X-TARGETDURATION:5")
    assert edit.text.rstrip().endswith("#EXT-X-ENDLIST")
    assert edit.spans == [(10_000, 12_000)]


def test_start_offset_shifts_the_time_axis():
    # A stream started at 60s: segment 0 covers 60-65s, so a cut at 66-69s removes segment 1.
    edit = edit_playlist(_playlist(4), [(66_000, 69_000)], offset_ms=60_000)
    assert edit.removed == [1]
    assert edit.spans == [(65_000, 70_000)]


def test_removed_ms_totals_all_spans():
    edit = edit_playlist(_playlist(10), [(5_000, 10_000), (30_000, 40_000)])
    assert edit.removed_ms == 15_000


SPANS = [(10_000, 20_000), (50_000, 55_000)]


@pytest.mark.parametrize("original, edited", [
    (0, 0), (9_999, 9_999), (10_000, 10_000), (15_000, 10_000), (20_000, 10_000),
    (30_000, 20_000), (50_000, 40_000), (60_000, 45_000),
])
def test_original_to_edited_collapses_removed_time(original, edited):
    assert original_to_edited(original, SPANS) == edited


@pytest.mark.parametrize("edited, original", [
    (0, 0), (9_999, 9_999), (10_000, 20_000), (20_000, 30_000), (40_000, 55_000), (45_000, 60_000),
])
def test_edited_to_original_skips_over_removed_time(edited, original):
    assert edited_to_original(edited, SPANS) == original


def test_mapping_round_trips_outside_removed_spans():
    for t in (0, 5_000, 25_000, 49_000, 70_000):
        assert edited_to_original(original_to_edited(t, SPANS), SPANS) == t
