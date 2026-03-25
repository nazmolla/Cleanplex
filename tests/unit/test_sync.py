"""Unit tests for sync.py — hash caching and helper functions."""

from __future__ import annotations

import tempfile
import os
import hashlib

import pytest

from cleanplex import sync


@pytest.fixture(autouse=True)
def clear_hash_cache():
    sync._hash_cache.clear()
    yield
    sync._hash_cache.clear()


# ── _parse_repo_slug ───────────────────────────────────────────────────────────

def test_parse_repo_slug_plain():
    assert sync._parse_repo_slug("owner/repo") == "owner/repo"


def test_parse_repo_slug_strips_github_url():
    assert sync._parse_repo_slug("https://github.com/owner/repo") == "owner/repo"


def test_parse_repo_slug_strips_trailing_slash():
    assert sync._parse_repo_slug("owner/repo/") == "owner/repo"


def test_parse_repo_slug_none_returns_empty():
    assert sync._parse_repo_slug(None) == ""


# ── _github_headers ───────────────────────────────────────────────────────────

def test_github_headers_includes_accept():
    headers = sync._github_headers(None)
    assert headers["Accept"] == "application/vnd.github+json"


def test_github_headers_with_token_includes_auth():
    headers = sync._github_headers("mytoken")
    assert headers["Authorization"] == "Bearer mytoken"


def test_github_headers_without_token_no_auth():
    headers = sync._github_headers(None)
    assert "Authorization" not in headers


# ── _segment_blob_path ─────────────────────────────────────────────────────────

def test_segment_blob_path_uses_first_two_chars_as_prefix():
    path = sync._segment_blob_path("abcdef1234")
    assert path == "segments/ab/abcdef1234.json"


def test_segment_blob_path_short_hash():
    # Hashes shorter than 2 chars fall back to "00" prefix per the implementation
    path = sync._segment_blob_path("a")
    assert path == "segments/00/a.json"


# ── compute_file_hash ──────────────────────────────────────────────────────────

def test_compute_file_hash_returns_sha256():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello world")
        tmp_path = f.name
    try:
        expected = hashlib.sha256(b"hello world").hexdigest()
        result = sync.compute_file_hash(tmp_path)
        assert result == expected
    finally:
        os.unlink(tmp_path)


def test_compute_file_hash_caches_result():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"data")
        tmp_path = f.name
    try:
        h1 = sync.compute_file_hash(tmp_path)
        # Should be served from cache now
        h2 = sync.compute_file_hash(tmp_path)
        assert h1 == h2
        # Cache should have exactly 1 entry
        assert len(sync._hash_cache) == 1
    finally:
        os.unlink(tmp_path)


def test_compute_file_hash_recomputes_after_size_change():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"original")
        tmp_path = f.name
    try:
        h1 = sync.compute_file_hash(tmp_path)
        # Overwrite with different content (changes size and mtime)
        with open(tmp_path, "wb") as f2:
            f2.write(b"modified content here")
        h2 = sync.compute_file_hash(tmp_path)
        assert h1 != h2
    finally:
        os.unlink(tmp_path)


def test_compute_file_hash_returns_empty_for_missing():
    result = sync.compute_file_hash("/nonexistent/path/file.mkv")
    assert result == ""


# ── compute_title_hash ─────────────────────────────────────────────────────────

def test_compute_title_hash_is_deterministic():
    h1 = sync.compute_title_hash("movie.mkv", 5400000)
    h2 = sync.compute_title_hash("movie.mkv", 5400000)
    assert h1 == h2


def test_compute_title_hash_differs_on_different_input():
    h1 = sync.compute_title_hash("a.mkv", 1000)
    h2 = sync.compute_title_hash("b.mkv", 1000)
    assert h1 != h2
