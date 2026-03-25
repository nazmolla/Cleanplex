"""
Segment library sharing: file hashing, sync coordination, and data aggregation.

⚠️  MANUAL OPERATIONS ONLY
This module supports user-initiated sync operations (upload/download).
No automatic scheduling or background jobs trigger these functions.
All sync is completely under user control via API endpoints.
"""

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .database import (
    get_local_library_for_sync,
    get_segment_library_entries_by_hash,
    upsert_segment_library_entry,
    get_sync_metadata,
    update_sync_last_time,
)
from .logger import get_logger

logger = get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_SEGMENTS_DIR = "segments"
DEFAULT_SYNC_GITHUB_REPO = "nazmolla/cleanplex-segments"

# In-process file hash cache keyed by (path, size, mtime) — avoids full SHA256
# re-hashing of unchanged large media files across repeated sync runs.
# Value: sha256 hex string.
_hash_cache: dict[tuple[str, int, float], str] = {}


def _parse_repo_slug(repo: str | None) -> str:
    value = (repo or "").strip()
    if value.startswith("https://github.com/"):
        value = value.replace("https://github.com/", "", 1)
    value = value.strip("/")
    return value


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Cleanplex-Sync",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _segment_blob_path(file_hash: str) -> str:
    prefix = file_hash[:2] if len(file_hash) >= 2 else "00"
    return f"{GITHUB_SEGMENTS_DIR}/{prefix}/{file_hash}.json"


async def _github_get_json_file(
    repo_slug: str,
    path: str,
    token: str | None,
    client: httpx.AsyncClient,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch a JSON file from GitHub Contents API. Caller provides shared client."""
    url = f"{GITHUB_API_BASE}/repos/{repo_slug}/contents/{path}"
    resp = await client.get(url, headers=_github_headers(token))
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()

    payload = resp.json()
    encoded = payload.get("content", "")
    decoded = base64.b64decode(encoded).decode("utf-8") if encoded else "{}"
    return json.loads(decoded), payload.get("sha")


async def _github_put_json_file(
    repo_slug: str,
    path: str,
    token: str,
    content_obj: dict[str, Any],
    message: str,
    sha: str | None,
    client: httpx.AsyncClient,
) -> None:
    """Write a JSON file to GitHub Contents API. Caller provides shared client."""
    url = f"{GITHUB_API_BASE}/repos/{repo_slug}/contents/{path}"
    encoded = base64.b64encode(json.dumps(content_obj, indent=2).encode("utf-8")).decode("ascii")
    body: dict[str, Any] = {"message": message, "content": encoded}
    if sha:
        body["sha"] = sha
    resp = await client.put(url, headers=_github_headers(token), json=body)
    resp.raise_for_status()


def compute_file_hash(file_path: str | Path) -> str:
    """Compute SHA256 hash of a file, using a (path, size, mtime) cache to skip
    re-hashing files that have not changed since the last sync run."""
    file_path = Path(file_path)
    try:
        stat = file_path.stat()
        cache_key = (str(file_path), stat.st_size, stat.st_mtime)
        if cache_key in _hash_cache:
            return _hash_cache[cache_key]

        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        digest = sha256_hash.hexdigest()
        _hash_cache[cache_key] = digest
        return digest
    except FileNotFoundError:
        logger.warning("File not found for hashing: %s", file_path)
        return ""
    except Exception as exc:
        logger.error("Error computing file hash: %s", exc)
        return ""


def compute_title_hash(file_name: str, duration_ms: int) -> str:
    """
    Compute a quick hash based on filename and duration.
    Used as a secondary lookup key for matching without expensive file hashing.
    """
    key = f"{file_name}:{duration_ms}"
    return hashlib.md5(key.encode()).hexdigest()


async def prepare_segments_for_upload(instance_name: str) -> dict[str, dict[str, Any]]:
    """
    Gather all local segments, compute file hashes, and prepare for upload.
    
    Returns: {
        file_hash: {
            "file_name": str,
            "file_size": int,
            "duration_ms": int,
            "segments": [{start_ms, end_ms, confidence, labels}, ...],
            "titles": ["title1", "title2"] (in case multiple versions exist)
        }
    }
    """
    upload_data = {}
    local_library = await get_local_library_for_sync()
    
    for item in local_library:
        file_path = item["file_path"]
        if not file_path:
            logger.warning(f"Skipping {item['title']}: no file path")
            continue
        
        # Compute file hash
        file_hash = compute_file_hash(file_path)
        if not file_hash:
            logger.warning(f"Skipping {item['title']}: could not compute file hash")
            continue
        
        # Get file metadata
        file_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
        
        # Build segment list (strip unnecessary fields)
        segments = []
        for seg in item["segments"]:
            segments.append({
                "start_ms": seg["start_ms"],
                "end_ms": seg["end_ms"],
                "confidence": seg["confidence"],
                "labels": seg["labels"],
            })
        
        # Store in upload data, handling potential duplicate hashes
        if file_hash not in upload_data:
            upload_data[file_hash] = {
                "file_name": item["title"],
                "file_size": file_size,
                "duration_ms": 0,  # Will be updated if available
                "segments": segments,
                "titles": [item["title"]],
                "source_instance": instance_name,
            }
        else:
            # Merge with existing entry
            upload_data[file_hash]["titles"].append(item["title"])
            upload_data[file_hash]["segments"].extend(segments)
    
    logger.info(f"Prepared {len(upload_data)} files for sync upload")
    return upload_data


async def push_segments_to_library(
    instance_name: str,
    upload_data: dict[str, dict[str, Any]],
) -> int:
    """Push local segments into the shared GitHub repo and mirror locally.

    Uses a single shared AsyncClient per operation and a semaphore to bound
    concurrent GitHub API calls to 5, staying well within rate limits.
    Returns the number of file entries updated.
    """
    sync_config = await get_sync_config()
    repo_slug = DEFAULT_SYNC_GITHUB_REPO
    github_token = (
        ((sync_config or {}).get("github_token") or os.environ.get("CLEANPLEX_SYNC_GITHUB_TOKEN") or "").strip()
    )

    if not repo_slug:
        raise RuntimeError("Sync repository is not configured")
    if not github_token:
        raise RuntimeError("Upload requires CLEANPLEX_SYNC_GITHUB_TOKEN on the server")

    now_iso = datetime.now(timezone.utc).isoformat()
    # Semaphore caps concurrent GitHub requests; stays under abuse-detection limits.
    sem = asyncio.Semaphore(5)

    async def _upload_one(file_hash: str, data: dict) -> None:
        async with sem:
            path = _segment_blob_path(file_hash)
            existing_doc, existing_sha = await _github_get_json_file(
                repo_slug, path, github_token, http_client
            )
            if not existing_doc:
                existing_doc = {"file_hash": file_hash, "sources": {}, "created_at": now_iso}

            existing_doc.setdefault("sources", {})
            existing_doc["sources"][instance_name] = {
                "file_name": data["file_name"],
                "file_size": data["file_size"],
                "duration_ms": data.get("duration_ms", 0),
                "segments": data["segments"],
                "titles": data.get("titles", [data["file_name"]]),
                "updated_at": now_iso,
            }
            existing_doc["updated_at"] = now_iso

            await _github_put_json_file(
                repo_slug=repo_slug,
                path=path,
                token=github_token,
                content_obj=existing_doc,
                message=f"sync: update segments {file_hash[:12]} from {instance_name}",
                sha=existing_sha,
                client=http_client,
            )

            # Keep local mirror for diagnostics/offline view.
            segments_json = json.dumps(data["segments"], default=str)
            await upsert_segment_library_entry(
                file_hash=file_hash,
                file_name=data["file_name"],
                file_size=data["file_size"],
                duration_ms=data.get("duration_ms", 0),
                segments_json=segments_json,
                source_instance=instance_name,
                confidence_level="local",
            )

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        await asyncio.gather(*(_upload_one(h, d) for h, d in upload_data.items()))

    count = len(upload_data)
    logger.info("Uploaded %d segment entries to GitHub repo %s", count, repo_slug)
    return count


async def fetch_cloud_segments(file_hashes: list[str]) -> dict[str, list[dict]]:
    """
    Fetch cloud segments for given file hashes.
    
    Returns: {
        file_hash: [
            {segments, source_instance, confidence_level, created_at},
            ...
        ]
    }
    """
    if not file_hashes:
        return {}
    
    sync_config = await get_sync_config()
    repo_slug = DEFAULT_SYNC_GITHUB_REPO
    github_token = (((sync_config or {}).get("github_token") or os.environ.get("CLEANPLEX_SYNC_GITHUB_TOKEN") or "").strip())

    if not repo_slug:
        raise RuntimeError("Sync repository is not configured")

    result: dict[str, list[dict]] = {}
    sem = asyncio.Semaphore(5)

    async def _fetch_one(file_hash: str, http_client: httpx.AsyncClient) -> None:
        async with sem:
            path = _segment_blob_path(file_hash)
            doc, _ = await _github_get_json_file(repo_slug, path, github_token or None, http_client)

        if not doc:
            entries = await get_segment_library_entries_by_hash(file_hash)
            result[file_hash] = [
                {
                    "segments": json.loads(entry["segments_json"]),
                    "source_instance": entry["source_instance"],
                    "confidence_level": entry["confidence_level"],
                    "created_at": entry["created_at"],
                }
                for entry in entries
            ]
            return

        sources = doc.get("sources", {}) if isinstance(doc, dict) else {}
        merged_sources: list[dict[str, Any]] = []
        for source_instance, source_payload in sources.items():
            if not isinstance(source_payload, dict):
                continue
            merged_sources.append({
                "segments": source_payload.get("segments", []),
                "source_instance": source_instance,
                "confidence_level": "shared",
                "created_at": source_payload.get("updated_at") or doc.get("updated_at"),
            })
        result[file_hash] = merged_sources

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        await asyncio.gather(*(_fetch_one(h, http_client) for h in file_hashes))

    logger.info("Fetched cloud segments for %d files", len(file_hashes))
    return result


async def get_local_file_hashes() -> list[str]:
    """Return SHA256 hashes for all locally scanned files."""
    hashes: list[str] = []
    local_library = await get_local_library_for_sync()
    for item in local_library:
        file_hash = compute_file_hash(item.get("file_path", ""))
        if file_hash:
            hashes.append(file_hash)
    return list(dict.fromkeys(hashes))


async def is_sync_enabled() -> bool:
    """Check if segment library sharing is enabled."""
    sync_config = await get_sync_metadata()
    return sync_config is not None and bool(sync_config.get("sync_enabled"))


async def get_sync_config() -> dict[str, Any] | None:
    """Get current sync configuration."""
    return await get_sync_metadata()


async def mark_sync_complete() -> None:
    """Update last sync timestamp."""
    await update_sync_last_time()
    logger.info("Sync completed, updated last_sync_time")
