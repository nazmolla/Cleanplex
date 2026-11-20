"""Not Rated / Unrated count as NR in the scan rating filter (#149)."""

from __future__ import annotations

import pytest

import cleanplex.scanner as scanner


@pytest.mark.parametrize("raw, expected", [
    ("Not Rated", "NR"), ("not rated", "NR"), ("  Unrated ", "NR"), ("NR", "NR"), ("nr", "NR"),
    ("TV-14", "TV-14"), (" PG-13 ", "PG-13"), ("", ""), (None, ""),
])
def test_normalize_rating(raw, expected):
    assert scanner.normalize_rating(raw) == expected


@pytest.mark.parametrize("rating", ["Not Rated", "Unrated", "NR", "nr"])
def test_the_nr_entry_admits_every_spelling_of_unrated(rating):
    assert scanner.rating_allowed(rating, {"R", "NR"}) is True


def test_a_list_holding_not_rated_admits_nr_too():
    assert scanner.rating_allowed("NR", {"Not Rated"}) is True


def test_other_ratings_still_have_to_be_listed_verbatim():
    assert scanner.rating_allowed("TV-14", {"R", "NR"}) is False
    assert scanner.rating_allowed("TV-14", {"TV-14"}) is True
    assert scanner.rating_allowed("PG", {"PG-13"}) is False


def test_a_blank_rating_matches_only_a_blank_entry():
    assert scanner.rating_allowed("", {"R", "NR"}) is False
    assert scanner.rating_allowed(None, {"R", ""}) is True


def test_an_empty_list_scans_everything():
    assert scanner.rating_allowed("Not Rated", set()) is True
    assert scanner.rating_allowed("anything", []) is True


def test_a_not_rated_title_is_eligible_when_nr_is_listed():
    job = {"library_id": "26", "content_rating": "Not Rated", "ignored": 0}
    assert scanner.is_scan_eligible(job, set(), {"R", "NR"}) is True


def test_a_not_rated_title_is_not_eligible_when_only_other_ratings_are_listed():
    job = {"library_id": "26", "content_rating": "Not Rated", "ignored": 0}
    assert scanner.is_scan_eligible(job, set(), {"R", "TV-MA"}) is False


@pytest.mark.parametrize("raw, expected", [
    ("eg/G", "G"), ("eg/8", "PG"), ("eg/12", "PG-13"), ("eg/13", "PG-13"), ("eg/14", "PG-13"),
    ("eg/16", "R"), ("eg/18", "R"), ("eg/R", "R"), ("EG/18+", "R"),
    ("eg/Egypt", "NR"), ("eg/أفلام صوت الفن - soutelfan", "NR"),
])
def test_egyptian_ratings_map_onto_the_us_scale(raw, expected):
    """#219: the AR library's eg/ ratings are checked with the same Scan ratings list."""
    assert scanner.normalize_rating(raw) == expected


def test_an_egyptian_g_is_out_of_scope_when_g_is_unchecked():
    assert scanner.rating_allowed("eg/G", {"R", "PG-13"}) is False
    assert scanner.rating_allowed("eg/16", {"R"}) is True
