"""Background job processing for long-running operations."""

import json
from .logger import get_logger
from . import database as db

logger = get_logger(__name__)

async def recover_stale_jobs() -> None:
    """Mark any jobs still in 'running' state as failed on startup.

    A job stuck in 'running' means the process was killed mid-flight; it can
    never complete now, so we surface it as failed rather than leaving it orphaned.
    """
    recovered = await db.fail_unfinished_bg_jobs("Process restarted while job was running")
    if recovered:
        logger.warning("Recovered %d stale background job(s) left in running/queued state", recovered)


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
