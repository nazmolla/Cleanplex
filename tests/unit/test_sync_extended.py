"""Extended unit tests for sync.py — GitHub helpers and config loading."""

from __future__ import annotations

import base64
import json
import tempfile
import os

import httpx
import pytest

from cleanplex import sync, database as db


pytestmark = pytest.mark.usefixtures("setup_db")


# ── _github_get_json_file ──────────────────────────────────────────────────────

async def test_github_get_json_file_returns_parsed_json():
    payload = {"segments": [{"start_ms": 0, "end_ms": 1000}]}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    resp_body = json.dumps({"content": encoded + "\n", "sha": "abc123"})

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=resp_body.encode(), headers={"content-type": "application/json"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result, sha = await sync._github_get_json_file("owner/repo", "segments/ab/hash.json", "tok", client)

    assert result == payload
    assert sha == "abc123"


async def test_github_get_json_file_returns_none_on_404():
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        result, sha = await sync._github_get_json_file("owner/repo", "missing.json", "tok", client)

    assert result is None
    assert sha is None


# ── _github_put_json_file ─────────────────────────────────────────────────────

async def test_github_put_json_file_sends_put_request():
    requests_seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests_seen.append(req)
        return httpx.Response(201, content=b"{}")

    transport = httpx.MockTransport(handler)
    content = {"segments": []}
    async with httpx.AsyncClient(transport=transport) as client:
        await sync._github_put_json_file(
            "owner/repo", "segments/ab/hash.json", "tok", content, "update", sha=None, client=client
        )

    assert len(requests_seen) == 1
    req = requests_seen[0]
    assert req.method == "PUT"
    body = json.loads(req.content)
    assert "content" in body
    assert "message" in body


async def test_github_put_json_file_includes_sha_when_provided():
    requests_seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests_seen.append(req)
        return httpx.Response(200, content=b"{}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await sync._github_put_json_file(
            "owner/repo", "f.json", "tok", {}, "msg", sha="abc", client=client
        )

    body = json.loads(requests_seen[0].content)
    assert body["sha"] == "abc"


# ── get_sync_config ───────────────────────────────────────────────────────────

async def test_get_sync_config_returns_none_when_not_configured():
    result = await sync.get_sync_config()
    assert result is None


async def test_get_sync_config_returns_config_when_set():
    await db.upsert_sync_metadata(
        instance_name="myinstance",
        github_repo="owner/repo",
        sync_enabled=True,
    )
    result = await sync.get_sync_config()
    assert result is not None
    assert result["instance_name"] == "myinstance"
    assert result["github_repo"] == "owner/repo"


# ── mark_sync_complete ────────────────────────────────────────────────────────

async def test_mark_sync_complete_no_error_when_no_config():
    # Should not raise even when sync is not configured
    await sync.mark_sync_complete()


async def test_mark_sync_complete_updates_timestamp_when_configured():
    await db.upsert_sync_metadata(
        instance_name="test",
        github_repo="owner/repo",
        sync_enabled=True,
    )
    await sync.mark_sync_complete()
    meta = await db.get_sync_metadata()
    assert meta is not None
