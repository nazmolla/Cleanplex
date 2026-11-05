"""Copy-mode (not re-encoded) cuts in the stream proxy, bounded by the file's keyframes (#184)."""

from cleanplex import database as db
from cleanplex.stream_proxy import edit_playlist

# Keyframes of a real 1080p file (Abominable) around the first minutes, ms, file time.
KF = [42, 3592, 8508, 13430, 18352, 23273, 28195, 33116, 38038, 42960, 47881, 52803, 57724]


def _playlist(n: int, seg_s: int = 10) -> str:
    lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{seg_s}"]
    for i in range(n):
        lines += [f"#EXTINF:{seg_s}, nodesc", f"{i:05d}.ts"]
    return "\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n"


def test_without_keyframes_only_nominally_overlapping_segments_go():
    edit = edit_playlist(_playlist(6), [(21_000, 24_000)])
    assert edit.removed == [2]  # nominal 20-30 s


def test_copy_mode_also_drops_a_segment_whose_real_start_may_reach_into_the_range():
    # Segment 1 is nominally 10-20 s but really runs to the first keyframe at or after 20 s
    # (23.273 s), so a range starting at 21 s reaches it: nominal-only cutting would leak.
    edit = edit_playlist(_playlist(6), [(21_000, 24_000)], keyframes=KF)
    assert edit.removed == [1, 2]


def test_copy_mode_respects_the_early_boundary_tolerance():
    # Plex started a segment on a keyframe 1.2 s before its nominal time (measured). Here
    # segment 3 (nominal 30 s) may start on the keyframe at 29 s, inside a range ending at
    # 29.5 s, so it must go too.
    # Segment 1 goes as well: segment 2 may start at 19.5 s or 29 s, so segment 1 may run to 29 s.
    keyframes = [0, 9000, 19500, 29000, 31000, 41000, 51000]
    edit = edit_playlist(_playlist(6), [(28_800, 29_500)], keyframes=keyframes)
    assert edit.removed == [1, 2, 3]


def test_copy_mode_spans_stay_nominal_for_the_player_clock():
    edit = edit_playlist(_playlist(6), [(21_000, 24_000)], keyframes=KF)
    assert edit.spans == [(10_000, 30_000)]


async def test_flagged_single_file_titles_are_queued_for_indexing_once(setup_db):
    # Single-file titles store their one path as a JSON list (what the library pass writes).
    await db.upsert_scan_job("g1", "Film", "/m/film.mkv", "71455", "1", "Movies", part_files='["/m/film.mkv"]')
    await db.upsert_scan_job("g2", "Parts", "/m/a.mkv", "71456", "1", "Movies", part_files='["/m/a.mkv","/m/b.mkv"]')
    await db.insert_segment("g1", "Film", 1000, 2000, 0.9, "", "X", severity="high")
    await db.insert_segment("g2", "Parts", 1000, 2000, 0.9, "", "X", severity="high")

    assert await db.get_flagged_files_without_keyframes(10) == ["/m/film.mkv"]
    await db.put_keyframes("/m/film.mkv", 10, 20, [0, 5000])
    assert await db.get_flagged_files_without_keyframes(10) == []
    assert await db.get_keyframes("/m/film.mkv", 10, 20) == [0, 5000]
    assert await db.get_keyframes("/m/film.mkv", 11, 20) is None  # file changed
    assert await db.get_file_for_rating_key("71456") is None  # multi-part: re-encode
    assert await db.get_file_for_rating_key("71455") == "/m/film.mkv"


async def test_copy_mode_needs_exactly_one_file(setup_db, monkeypatch):
    from cleanplex import stream_proxy
    from cleanplex import keyframe_index

    await db.upsert_scan_job("g1", "Film", "/m/film.mkv", "71455", "1", "Movies", part_files='["/m/film.mkv"]')
    async def kf(path):
        return [0, 5000]
    monkeypatch.setattr(keyframe_index, "keyframes_for", kf)

    assert await stream_proxy.db_keyframes("71455") is None  # parts unknown yet: re-encode
    await db.replace_title_parts([(1, "71455")])
    assert await stream_proxy.db_keyframes("71455") == [0, 5000]
    await db.replace_title_parts([(2, "71455")])  # a second version appears
    assert await stream_proxy.db_keyframes("71455") is None
