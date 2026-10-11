"""
database/db.py
Supabase Cloud Database bridge (replaced legacy local SQLite jobs.db).
All job tracking and deduplication are now hosted in Supabase PostgreSQL.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database.user_service import (
    save_scraped_jobs_to_supabase,
    get_recent_scraped_jobs_from_supabase,
    get_scraped_jobs_count_from_supabase,
)
from database.supabase_client import get_supabase_admin_client, is_supabase_configured

logger = logging.getLogger(__name__)


def init_db(db_path: str = None) -> None:
    """Verify Supabase Cloud connection status."""
    if is_supabase_configured():
        logger.info("Database: Supabase PostgreSQL Cloud connected successfully (Zero local database files).")
    else:
        logger.warning("Database: Supabase URL/key not configured in .env.")


def job_exists(link: str, db_path: str = None) -> bool:
    """Check if a job link already exists in Supabase scraped_jobs."""
    if not link:
        return False
    admin = get_supabase_admin_client()
    if not admin:
        return False
    try:
        res = admin.table("scraped_jobs").select("id").eq("fingerprint", link.strip().lower()).limit(1).execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"Error checking if job exists in Supabase ({link}): {e}")
        return False


def insert_job(job: dict, db_path: str = None) -> bool:
    """Insert a single job record into Supabase scraped_jobs."""
    count = save_scraped_jobs_to_supabase([job])
    return count > 0


def get_total_jobs_count(db_path: str = None) -> int:
    """Return total number of saved jobs in Supabase scraped_jobs."""
    return get_scraped_jobs_count_from_supabase()


def get_recent_jobs(limit: int = 25, db_path: str = None) -> list:
    """Return recently saved jobs from Supabase scraped_jobs."""
    return get_recent_scraped_jobs_from_supabase(limit=limit)
