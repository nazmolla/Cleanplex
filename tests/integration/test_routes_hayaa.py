"""Hayaa linking routes (#221)."""

from __future__ import annotations

import pytest

from cleanplex import hayaa

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture(autouse=True)
def reset_state():
    yield
    hayaa._access, hayaa._link, hayaa._link_task, hayaa._http = None, None, None, None


async def test_status_starts_unconfigured_and_unlinked(http_client):
    resp = await http_client.get("/api/hayaa/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False and body["linked"] is False and body["link"] is None


async def test_settings_save_https_urls(http_client):
    resp = await http_client.put("/api/hayaa/settings",
                                 json={"api_url": "https://api.test/", "auth_url": "https://auth.test"})
    assert resp.status_code == 200
    assert resp.json()["configured"] is True and resp.json()["api_url"] == "https://api.test"


async def test_settings_reject_plain_http_to_another_host(http_client):
    resp = await http_client.put("/api/hayaa/settings",
                                 json={"api_url": "http://api.test", "auth_url": "https://auth.test"})
    assert resp.status_code == 422


async def test_settings_require_both_fields(http_client):
    resp = await http_client.put("/api/hayaa/settings", json={"api_url": "https://api.test"})
    assert resp.status_code == 422


async def test_link_without_urls_is_a_400(http_client):
    resp = await http_client.post("/api/hayaa/link")
    assert resp.status_code == 400


async def test_unlink_when_not_linked_is_harmless(http_client):
    resp = await http_client.post("/api/hayaa/unlink")
    assert resp.status_code == 200 and resp.json()["linked"] is False
