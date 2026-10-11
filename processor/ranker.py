import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GEMINI_API_KEY
from llm.gemini_client import evaluate_jobs_with_gemini

logger = logging.getLogger(__name__)


def get_top_jobs(jobs: list, profile: dict = None) -> list:
    """
    Sends fresh jobs to Gemini AI for ranking.
    When a candidate profile is supplied, ranking is personalized.
    Returns the top 9 most relevant jobs.
    Falls back to the first 9 unranked jobs if Gemini fails or API key is missing.

    Args:
        jobs: List of job dicts scraped from LinkedIn/Naukri.
        profile: Optional candidate profile dict (skills, keywords, experience_level, roles).
    """
    if not jobs:
        logger.info("No jobs in cache to rank.")
        return []

    # Gemini has a 1M+ token context window, comfortably handling larger batches
    MAX_TO_SEND = 100
    jobs_to_evaluate = jobs[:MAX_TO_SEND]

    if GEMINI_API_KEY:
        try:
            logger.info(f"Sending {len(jobs_to_evaluate)} jobs to Google Gemini for ranking...")
            top_jobs = evaluate_jobs_with_gemini(jobs_to_evaluate, profile=profile)
            if top_jobs:
                top_jobs = top_jobs[:9]
                logger.info(f"Gemini ranked top {len(top_jobs)} jobs.")
                return top_jobs
        except Exception as e:
            logger.error(f"Gemini ranking failed: {e}. Falling back to first 9 unranked jobs.")
    else:
        logger.warning("GEMINI_API_KEY not set. Using first 9 unranked jobs.")

    return jobs[:9]
