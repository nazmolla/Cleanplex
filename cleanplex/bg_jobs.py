"""Background job processing for long-running operations."""

import asyncio
import json
from .logger import get_logger
from . import database as db
from .sync import prepare_segments_for_upload, push_segments_to_library, mark_sync_complete, get_sync_config

logger = get_logger(__name__)

# Global task tracking
_running_tasks = {}


async def process_upload_job(job_id: int) -> None:
    """
    Process an upload job in the background.
    Updates job status and result in the database as it progresses.
    """
    try:
        await db.update_bg_job(job_id, status='running', progress=0)
        
        # Get sync config
        config = await get_sync_config()
        if not config:
            raise Exception("Sync not configured")
        
        instance_name = config.get("instance_name", "unknown")
        
        # Step 1: Gather segments (20% progress)
        logger.info(f"[Job {job_id}] Gathering segments for upload...")
        await db.update_bg_job(job_id, progress=20)
        
        upload_data = await prepare_segments_for_upload(instance_name)
        
        if not upload_data:
            logger.warning(f"[Job {job_id}] No segments to upload")
            result = {
                "status": "no_data",
                "files_processed": 0,
                "entries_updated": 0,
                "message": "No segments to upload",
            }
            await db.update_bg_job(
                job_id,
                status='completed',
                progress=100,
                result=json.dumps(result),
            )
            return
        
        # Step 2: Upload to GitHub (60% progress)
        logger.info(f"[Job {job_id}] Uploading {len(upload_data)} files to GitHub...")
        await db.update_bg_job(job_id, progress=60)
        
        entries_count = await push_segments_to_library(instance_name, upload_data)
        
        # Step 3: Mark complete (90% progress)
        await db.update_bg_job(job_id, progress=90)
        logger.info(f"[Job {job_id}] Marking sync complete...")
        
        await mark_sync_complete()
        
        # Success!
        result = {
            "status": "success",
            "files_processed": len(upload_data),
            "entries_updated": entries_count,
            "message": f"Uploaded {len(upload_data)} files to GitHub segment library",
        }
        
        logger.info(f"[Job {job_id}] Upload complete: {entries_count} entries")
        await db.update_bg_job(
            job_id,
            status='completed',
            progress=100,
            result=json.dumps(result),
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[Job {job_id}] Upload failed: {error_msg}")
        await db.update_bg_job(
            job_id,
            status='failed',
            progress=0,
            error=error_msg,
        )
    finally:
        # Clean up task reference
        _running_tasks.pop(job_id, None)


async def enqueue_upload_job() -> int:
    """
    Enqueue an upload job and return the job ID.
    The job will be processed in the background without blocking the request.
    """
    # Create job record
    job_id = await db.create_bg_job('upload')
    
    # Create async task (won't block the request)
    task = asyncio.create_task(process_upload_job(job_id))
    _running_tasks[job_id] = task
    
    logger.info(f"Enqueued upload job {job_id}")
    return job_id


async def get_job_status(job_id: int) -> dict | None:
    """Get the status of a background job."""
    job = await db.get_bg_job(job_id)
    if not job:
        return None
    
    # Parse result data if present
    result_data = None
    if job.get('result_data'):
        try:
            result_data = json.loads(job['result_data'])
        except:
            pass
    
    return {
        'id': job['id'],
        'job_type': job['job_type'],
        'status': job['status'],
        'progress': job['progress_percent'],
        'error': job.get('error_message'),
        'result': result_data,
        'created_at': job.get('created_at'),
        'completed_at': job.get('completed_at'),
    }
