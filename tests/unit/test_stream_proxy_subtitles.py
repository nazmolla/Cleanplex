"""Subtitle re-timing for edited streams (#184)."""

from cleanplex.stream_proxy import shift_subtitles

SPANS = [(60_000, 90_000)]  # 30 s removed at 1:00-1:30


def test_srt_cues_after_a_cut_move_back_and_cues_inside_it_are_dropped():
    srt = (
        "1\n00:00:10,000 --> 00:00:12,000\nBefore\n\n"
        "2\n00:01:05,000 --> 00:01:07,000\nInside\n\n"
        "3\n00:02:00,500 --> 00:02:03,000\nAfter\n"
    )
    out = shift_subtitles(srt, SPANS)
    assert "Inside" not in out
    assert "00:00:10,000 --> 00:00:12,000" in out
    assert "00:01:30,500 --> 00:01:33,000\nAfter" in out


def test_webvtt_keeps_header_and_settings():
    vtt = "WEBVTT\n\n00:02:00.000 --> 00:02:01.000 align:start\nAfter\n"
    out = shift_subtitles(vtt, SPANS)
    assert out.startswith("WEBVTT")
    assert "00:01:30.000 --> 00:01:31.000 align:start" in out


def test_ass_dialogue_lines_are_retimed_and_styles_untouched():
    ass = (
        "[Script Info]\nTitle: x\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:01:10.00,0:01:12.00,Default,,0,0,0,,Inside\n"
        "Dialogue: 0,0:02:00.00,0:02:02.50,Default,,0,0,0,,After, with comma\n"
    )
    out = shift_subtitles(ass, SPANS)
    assert "Inside" not in out
    assert "Dialogue: 0,0:01:30.00,0:01:32.50,Default,,0,0,0,,After, with comma" in out
    assert out.startswith("[Script Info]")


def test_a_cue_straddling_the_cut_is_kept_and_clamped():
    srt = "1\n00:00:58,000 --> 00:01:02,000\nStraddle\n"
    out = shift_subtitles(srt, SPANS)
    assert "00:00:58,000 --> 00:01:00,000" in out


def test_no_spans_returns_the_file_unchanged():
    assert shift_subtitles("1\n00:00:01,000 --> 00:00:02,000\nx\n", []) == "1\n00:00:01,000 --> 00:00:02,000\nx\n"


def test_streamed_in_arbitrary_chunks_gives_the_same_result():
    """Plex sends subtitles progressively; chunk boundaries must not change the output (#184)."""
    from cleanplex.stream_proxy import SubtitleShifter

    ass = ("[Events]\nDialogue: 0,0:01:10.00,0:01:12.00,Default,,0,0,0,,Inside\n"
           "Dialogue: 0,0:02:00.00,0:02:02.50,Default,,0,0,0,,After\n")
    whole = shift_subtitles(ass, SPANS)
    shifter = SubtitleShifter(SPANS)
    pieces = [ass[i:i + 7] for i in range(0, len(ass), 7)]
    streamed = "".join(shifter.feed(p) for p in pieces) + shifter.close()
    assert streamed == whole


def test_srt_cue_number_of_a_dropped_cue_is_dropped_too():
    srt = "1\n00:01:05,000 --> 00:01:07,000\nInside\n\n2\n00:02:00,000 --> 00:02:01,000\nAfter\n"
    out = shift_subtitles(srt, SPANS)
    assert out.startswith("2\n00:01:30,000 --> 00:01:31,000\nAfter")
