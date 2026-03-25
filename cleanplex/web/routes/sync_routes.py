"""Web API routes for segment library synchronization."""

from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel

from ...sync import (
    DEFAULT_SYNC_GITHUB_REPO,
    prepare_segments_for_upload,
    push_segments_to_library,
    fetch_cloud_segments,
    get_local_file_hashes,
    is_sync_enabled,
    get_sync_config,
    mark_sync_complete,
    compute_file_hash,
)
from ...sync_merge import resolve_segments
from ...database import (
    get_sync_metadata,
    upsert_sync_metadata,
)
from ...bg_jobs import enqueue_upload_job, get_job_status
from ...logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


# ── Response Models ────────────────────────────────────────────────────────────

class SyncStatus(BaseModel):
    sync_enabled: bool
    instance_name: str | None
    github_repo: str | None
    conflict_resolution: str
    verified_threshold: int
    timing_tolerance_ms: int
    last_sync_time: str | None


class SegmentMergeResult(BaseModel):
    file_hash: str
    segments: list[dict]
    merge_stats: dict


class SyncUploadResponse(BaseModel):
    status: str
    files_processed: int
    entries_updated: int
    message: str


class SyncDownloadResponse(BaseModel):
    status: str
    results: dict[str, list]  # {file_hash: [segment, ...]}
    merge_results: dict  # {file_hash: merge_stats}


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=SyncStatus)
async def get_sync_status():
    """
    Get current sync configuration and status.
    Used by frontend to determine if sync is enabled and display sync settings.
    """
    metadata = await get_sync_metadata()
    
    if not metadata:
        return SyncStatus(
            sync_enabled=False,
            instance_name=None,
            github_repo=DEFAULT_SYNC_GITHUB_REPO,
            conflict_resolution="consensus",
            verified_threshold=2,
            timing_tolerance_ms=2000,
            last_sync_time=None,
        )
    
    return SyncStatus(
        sync_enabled=bool(metadata.get("sync_enabled", 0)),
        instance_name=metadata.get("instance_name"),
        github_repo=DEFAULT_SYNC_GITHUB_REPO,
        conflict_resolution=metadata.get("conflict_resolution", "consensus"),
        verified_threshold=metadata.get("verified_threshold", 2),
        timing_tolerance_ms=metadata.get("timing_tolerance_ms", 2000),
        last_sync_time=metadata.get("last_sync_time"),
    )


@router.post("/settings")
async def configure_sync(
    instance_name: str = Body(...),
    sync_enabled: bool = Body(False),
    conflict_resolution: str = Body("consensus"),
    verified_threshold: int = Body(2),
    timing_tolerance_ms: int = Body(2000),
):
    """
    Update sync configuration.
    Repository is fixed to the default crowdsourced repo.
    """
    if not instance_name or not instance_name.strip():
        raise HTTPException(status_code=400, detail="instance_name is required")

    current = await get_sync_metadata()
    existing_token = current.get("github_token") if current else None

    await upsert_sync_metadata(
        instance_name=instance_name,
        github_repo=DEFAULT_SYNC_GITHUB_REPO,
        github_token=existing_token,
        sync_enabled=sync_enabled,
        conflict_resolution=conflict_resolution,
        verified_threshold=verified_threshold,
        timing_tolerance_ms=timing_tolerance_ms,
    )
    
    logger.info(f"Updated sync config for instance: {instance_name}")
    
    return {
        "status": "success",
        "message": f"Sync configured for instance: {instance_name}",
    }


@router.post("/upload-segment-library")
async def upload_segment_library():
    """
    Enqueue a segment library upload job.
    
    Returns immediately with a job ID.
    Use /api/sync/job-status/{job_id} to check progress.
    
    ⚠️  MANUAL OPERATION ONLY - Never called automatically
    User must explicitly trigger from UI or API client
    """
    if not await is_sync_enabled():
        raise HTTPException(status_code=400, detail="Sync not enabled")
    
    config = await get_sync_config()
    if not config:
        raise HTTPException(status_code=500, detail="Sync not configured")
    
    try:
        # Enqueue the upload job (runs in background)
        job_id = await enqueue_upload_job()
        
        return {
            "status": "queued",
            "job_id": job_id,
            "message": f"Upload job {job_id} queued. Check status at /api/sync/job-status/{job_id}",
        }
    
    except Exception as e:
        logger.error(f"Failed to enqueue upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to enqueue upload: {str(e)}")


@router.get("/job-status/{job_id}")
async def get_upload_job_status(job_id: int):
    """
    Check the status of a background upload job.
    
    Returns:
    - status: 'running', 'completed', 'failed', or 'queued'
    - progress: 0-100 percent complete
    - result: Upload result if completed (files_processed, entries_updated, etc)
    - error: Error message if failed
    """
    job = await get_job_status(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    return job


@router.get("/download-segment-library", response_model=SyncDownloadResponse)
async def download_segment_library(
    file_hashes: str = Query(..., description="Comma-separated file hashes to download"),
):
    """
    Download and merge segments from cloud library for given file hashes.
    
    ⚠️  MANUAL OPERATION ONLY - Never called automatically
    User must explicitly request for specific files via UI or API
    
    Parameters:
    - file_hashes: Comma-separated list of SHA256 file hashes (your local files)
    
    Returns:
    - segments: Merged segments per file (local preference priority)
    - merge_results: Per-file statistics (verified count, source count, confidence level)
    
    Merge Process:
    1. Fetch all cloud sources for requested file hashes
    2. Apply conflict resolution (voting, confidence weighting, timing tolerance)
    3. Return merged segments with confidence level and source tracking
    4. Does NOT modify local database - only returns merged data for review
    """
    if not await is_sync_enabled():
        raise HTTPException(status_code=400, detail="Sync not enabled")
    
    config = await get_sync_config()
    if not config:
        raise HTTPException(status_code=500, detail="Sync not configured")
    
    try:
        # Parse hashes
        hash_list = [h.strip() for h in file_hashes.split(",") if h.strip()]
        if not hash_list:
            raise HTTPException(status_code=400, detail="No file hashes provided")
        
        # Fetch cloud segments
        cloud_sources = await fetch_cloud_segments(hash_list)
        
        # Resolve/merge each file
        results = {}
        merge_stats = {}
        
        for file_hash in hash_list:
            sources = cloud_sources.get(file_hash, [])
            
            if not sources:
                logger.info(f"No cloud segments found for {file_hash[:8]}...")
                results[file_hash] = []
                merge_stats[file_hash] = {"status": "not_found"}
                continue
            
            # Merge with local (if any exist)
            merged, stats = await resolve_segments(
                file_hash=file_hash,
                local_segments=[],  # Not fetching local here; this is for downloads
                cloud_sources=sources,
                timing_tolerance_ms=config.get("timing_tolerance_ms", 2000),
                verified_threshold=config.get("verified_threshold", 2),
                prefer_local=True,
            )
            
            results[file_hash] = merged
            merge_stats[file_hash] = stats
        
        await mark_sync_complete()
        
        logger.info(f"Downloaded and merged {len(hash_list)} files")
        
        return SyncDownloadResponse(
            status="success",
            results=results,
            merge_results=merge_stats,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download failed: {e}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


@router.get("/download-local-library", response_model=SyncDownloadResponse)
async def download_local_library():
    """
    Download crowdsourced segments for all local files and merge them.

    This is a convenience endpoint for the UI and remains manual-only.
    """
    if not await is_sync_enabled():
        raise HTTPException(status_code=400, detail="Sync not enabled")

    local_hashes = await get_local_file_hashes()
    if not local_hashes:
        return SyncDownloadResponse(status="no_data", results={}, merge_results={})

    return await download_segment_library(file_hashes=",".join(local_hashes))


@router.get("/conflicts")
async def get_conflicts():
    """
    Get detected conflicts (segments with different timings from multiple sources).
    
    Useful for reviewing disagreements between different instances/detectors.
    Helps identify detector variations or false positives.
    """
    if not await is_sync_enabled():
        raise HTTPException(status_code=400, detail="Sync not enabled")
    
    logger.info("Conflict list requested (not yet implemented)")
    
    return {
        "status": "not_implemented",
        "message": "Conflict detection will be available in Phase 2",
        "conflicts": [],
    }


@router.post("/test-hash")
async def test_file_hash(file_path: str = Body(...)):
    """
    Test file hashing for a given file path.
    Useful for debugging file identification issues.
    """
    try:
        file_hash = compute_file_hash(file_path)
        return {
            "file_path": file_path,
            "file_hash": file_hash,
            "status": "success",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Hashing failed: {str(e)}")
