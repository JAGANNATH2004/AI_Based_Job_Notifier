"""
processor/filter_jobs.py
Four-stage pre-filter before sending to Gemini AI:
  Stage 1 — Link check:      Must have a non-empty link.
  Stage 2 — Title keywords:  Must match at least one user-specified role / keyword.
  Stage 3 — Location:        Must match at least one user-specified location.
  Stage 4 — Experience:      Must require 0-1 year (freshers only).
"""
import re
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import JOB_LOCATIONS, JOB_ROLES

logger = logging.getLogger(__name__)

FRESHER_TERMS = [
    "fresher", "fresh", "entry", "intern", "trainee", "graduate"
]


def _is_within_experience(experience_str: str, target_exp: str = "fresher") -> bool:
    """
    Returns True if the job's experience requirement matches target_exp level.
    Supports 'fresher' (0-1 yr), '0-2 years', '1-3 years', '3-5 years', '5+ years', or 'any'.
    """
    if not experience_str:
        return True if target_exp == "any" else False

    exp_lower = experience_str.lower().strip()
    target_clean = (target_exp or "fresher").lower().strip()

    if target_clean in ["any", "all"]:
        return True

    numbers = [int(n) for n in re.findall(r'\d+', exp_lower)]

    if target_clean in ["fresher", "entry", "0-1 years", "0-1 year"]:
        if any(term in exp_lower for term in FRESHER_TERMS):
            return True
        if not numbers:
            return False
        return numbers[0] <= 1

    if target_clean in ["0-2 years", "junior"]:
        if any(term in exp_lower for term in FRESHER_TERMS) or "junior" in exp_lower:
            return True
        if not numbers:
            return False
        return numbers[0] <= 2

    if target_clean in ["1-3 years", "mid-level", "mid"]:
        if not numbers:
            return False
        min_e = numbers[0]
        max_e = numbers[1] if len(numbers) > 1 else min_e
        return not (max_e < 1 or min_e > 3)

    if target_clean in ["3-5 years", "senior"]:
        if not numbers:
            return "senior" in exp_lower
        min_e = numbers[0]
        max_e = numbers[1] if len(numbers) > 1 else min_e
        return not (max_e < 3 or min_e > 5)

    if target_clean in ["5+ years", "lead", "principal"]:
        if not numbers:
            return any(k in exp_lower for k in ["lead", "principal", "architect", "staff"])
        max_e = numbers[1] if len(numbers) > 1 else numbers[0]
        return max_e >= 5

    # Default permissive match if format is custom
    return True


def filter_basic_jobs(
    jobs: list,
    target_cities: list = None,
    job_roles: list = None,
    experience_level: str = "fresher"
) -> list:
    """
    Four-stage pre-filter before sending to Gemini AI:
    Stage 1 — Link check:      Must have a non-empty link.
    Stage 2 — Title keywords:  Must match at least one user-specified role or keyword.
    Stage 3 — Location:        Must be in one of the target cities (or Remote).
    Stage 4 — Experience:      Must match user-specified experience tier.
    """
    # Use user-specified cities or defaults
    cities_to_check = target_cities if target_cities else JOB_LOCATIONS
    cities_lower = [c.lower().strip() for c in cities_to_check if c.strip()]
    if "bengaluru" not in cities_lower and "bangalore" in cities_lower:
        cities_lower.append("bengaluru")
    if "bangalore" not in cities_lower and "bengaluru" in cities_lower:
        cities_lower.append("bangalore")

    # Extract keywords from target roles
    roles_to_check = job_roles if job_roles else JOB_ROLES
    keywords = set()
    for r in roles_to_check:
        clean_r = r.lower().strip()
        keywords.add(clean_r)
        # Add subwords with length >= 3 (e.g. 'python', 'developer', 'engineer', 'analyst')
        for word in re.findall(r'\b[a-zA-Z]{3,}\b', clean_r):
            keywords.add(word)

    relevant_jobs = []

    for job in jobs:
        title = job.get("title", "").strip()
        link = job.get("link", "").strip()
        location = job.get("location", "").strip().lower()
        experience = job.get("experience", "").strip()

        # Stage 1: Link check
        if not link:
            logger.debug(f"Dropped (no link): '{title}'")
            continue

        # Stage 2: Title keywords / roles check
        title_lower = title.lower()
        if keywords and not any(kw in title_lower for kw in keywords):
            logger.debug(f"Dropped (title): '{title}'")
            continue

        # Stage 3: Location check
        location_matches = (
            not cities_lower or
            any(city in location for city in cities_lower) or
            "remote" in location
        )
        if not location_matches:
            logger.debug(f"Dropped (location '{job.get('location')}'): '{title}'")
            continue

        # Stage 4: Experience check
        if not _is_within_experience(experience, target_exp=experience_level):
            logger.debug(f"Dropped (exp '{experience}' doesn't match '{experience_level}'): '{title}'")
            continue

        relevant_jobs.append(job)

    logger.info(
        f"Filter: {len(jobs)} scraped -> {len(relevant_jobs)} kept "
        f"({experience_level} exp | Locations: {cities_to_check} | Roles: {len(roles_to_check)} targets)"
    )
    return relevant_jobs
