"""
processor/deduplicator.py
Supabase Cloud-native deduplication logic to ensure only fresh, unnotified jobs are processed.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database.user_service import save_scraped_jobs_to_supabase, get_user_delivered_links

logger = logging.getLogger(__name__)


def deduplicate_and_store_jobs(jobs: list, user_id: str = None) -> list:
    """
    1. Deduplicates links within the batch in-memory.
    2. If a user_id is provided, filters out jobs previously delivered to that user in Supabase.
    3. Persists fresh listings into Supabase cloud table (public.scraped_jobs).
    """
    if not jobs:
        return []

    seen_links = set()
    unique_jobs = []

    for job in jobs:
        link = job.get("link", "").strip()
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        unique_jobs.append(job)

    # Filter out jobs previously delivered to this user
    if user_id:
        delivered_links = get_user_delivered_links(user_id)
        if delivered_links:
            unique_jobs = [j for j in unique_jobs if j.get("link") not in delivered_links]

    # Save to Supabase cloud master pool (public.scraped_jobs)
    inserted_count = save_scraped_jobs_to_supabase(unique_jobs)

    logger.info(f"Deduplication: {len(jobs)} scraped -> {len(unique_jobs)} fresh jobs.")
    logger.info(f"Supabase Cloud: Persisted {inserted_count} jobs to scraped_jobs.")
    return unique_jobs
