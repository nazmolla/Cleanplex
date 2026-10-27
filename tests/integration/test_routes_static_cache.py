"""Browsers revalidate the app shell and cache hashed assets for good (issue #94)."""

from __future__ import annotations

import pytest

from cleanplex.web.app import STATIC_DIR

pytestmark = pytest.mark.skipif(not (STATIC_DIR / "index.html").exists(), reason="frontend not built")


@pytest.mark.parametrize("path", ["/", "/library", "/segments"])
async def test_app_shell_is_always_revalidated(http_client, path):
    resp = await http_client.get(path)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


async def test_hashed_assets_are_cached_immutably(http_client):
    asset = next((STATIC_DIR / "assets").iterdir())

    resp = await http_client.get(f"/assets/{asset.name}")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_api_responses_keep_their_own_caching(http_client):
    resp = await http_client.get("/api/settings")

    assert "immutable" not in resp.headers.get("cache-control", "")
    assert resp.headers.get("cache-control") != "no-cache"
