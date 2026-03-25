"""
Segment library sharing: file hashing, sync coordination, and data aggregation.

⚠️  MANUAL OPERATIONS ONLY
This module supports user-initiated sync operations (upload/download).
No automatic scheduling or background jobs trigger these functions.
All sync is completely under user control via API endpoints.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from .database import (
    get_local_library_for_sync,
    get_segment_library_entries_by_hash,
    upsert_segment_library_entry,
    get_sync_metadata,
    update_sync_last_time,
)
from .logger import get_logger

logger = get_logger(__name__)


def compute_file_hash(file_path: str | Path) -> str:
    """
    Compute SHA256 hash of a file for unique identification.
    This is the primary key for cross-instance segment matching.
    """
    file_path = Path(file_path)
    sha256_hash = hashlib.sha256()
    
    try:
        with open(file_path, "rb") as f:
            # Read in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        logger.warning(f"File not found for hashing: {file_path}")
        return ""
    except Exception as e:
        logger.error(f"Error computing file hash: {e}")
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
    """
    Store uploaded segments in local library database.
    Returns: number of entries updated/inserted.
    """
    count = 0
    for file_hash, data in upload_data.items():
        segments_json = json.dumps(data["segments"], default=str)
        await upsert_segment_library_entry(
            file_hash=file_hash,
            file_name=data["file_name"],
            file_size=data["file_size"],
            duration_ms=data.get("duration_ms", 0),
            segments_json=segments_json,
            source_instance=instance_name,
            confidence_level="local",  # Local scans are always "local" confidence
        )
        count += 1
    
    logger.info(f"Stored {count} segment library entries from {instance_name}")
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
    
    result = {}
    for file_hash in file_hashes:
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
    
    logger.info(f"Fetched cloud segments for {len(file_hashes)} files")
    return result


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
