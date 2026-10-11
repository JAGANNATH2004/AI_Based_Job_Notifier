"""
llm/gemini_client.py
Ranks jobs.
"""
import json
import logging
import os
import sys
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)


def evaluate_jobs_with_gemini(jobs_batch: list, profile: dict = None) -> list:
    """
    Sends a batch of scraped jobs to the Google Gemini API for ranking.
    When a candidate profile is provided, personalizes the ranking based on skills,
    experience, and target roles.

    Args:
        jobs_batch: List of job dicts to rank.
        profile: Optional candidate profile dict (skills, keywords, experience_level, roles).

    Returns:
        List of up to 9 top-ranked job dicts.
    """
    if not jobs_batch:
        return []

    if not GEMINI_API_KEY:
        logger.warning(
            "GEMINI_API_KEY is not set in your .env file. "
            "Please add GEMINI_API_KEY=your_key to .env. Falling back to first 9 jobs."
        )
        return jobs_batch[:9]

    try:
        # Load prompt template
        template_path = os.path.join(os.path.dirname(__file__), "prompt_template.txt")
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        jobs_json = json.dumps(jobs_batch, indent=2)
        prompt = template.replace("{jobs_json}", jobs_json)

        # Prepend candidate profile if provided
        if profile and any([profile.get("skills"), profile.get("roles")]):
            skills_str = ", ".join(profile.get("skills", []))
            keywords_str = ", ".join(profile.get("keywords", []))
            roles_str = ", ".join(profile.get("roles", []))
            exp_str = profile.get("experience_level", "fresher")
            profile_block = (
                f"Candidate Profile (personalize ranking for this candidate):\n"
                f"  - Skills: {skills_str}\n"
                f"  - Domain Keywords: {keywords_str}\n"
                f"  - Experience Level: {exp_str}\n"
                f"  - Target Roles: {roles_str}\n\n"
            )
            prompt = profile_block + prompt

        model_name = GEMINI_MODEL or "gemini-2.5-flash"
        logger.info(f"Calling Gemini model '{model_name}' to rank {len(jobs_batch)} jobs...")

        api_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_name}:generateContent"
        )
        api_headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "systemInstruction": {
                "parts": [{
                    "text": (
                        "You are an expert AI recruiting assistant. "
                        "You ONLY output a valid JSON object with a single key 'jobs' containing an array of job objects. "
                        "No markdown fences. No explanation. Pure JSON only."
                    )
                }]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2
            }
        }

        response = requests.post(api_url, headers=api_headers, json=payload, timeout=90)

        # If model name failed with 404, 503 (high demand), or 429, fallback to alternates
        if response.status_code in [404, 503, 429]:
            for fallback_model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
                if fallback_model != model_name:
                    logger.warning(f"Model '{model_name}' returned status {response.status_code}. Retrying with '{fallback_model}'...")
                    fallback_url = (
                        f"https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{fallback_model}:generateContent"
                    )
                    response = requests.post(fallback_url, headers=api_headers, json=payload, timeout=90)
                    if response.ok:
                        break

        response.raise_for_status()
        data = response.json()

        # Extract generated content
        candidates = data.get("candidates", [])
        if not candidates:
            logger.warning("Gemini returned no candidates. Using fallback.")
            return jobs_batch[:9]

        content_parts = candidates[0].get("content", {}).get("parts", [])
        if not content_parts:
            logger.warning("Gemini candidate content has no parts. Using fallback.")
            return jobs_batch[:9]

        response_text = content_parts[0].get("text", "").strip()

        # Parse JSON
        parsed = json.loads(response_text)

        # Primary: expect {"jobs": [...]}
        if isinstance(parsed, dict):
            top_jobs = (
                parsed.get("jobs")
                or parsed.get("results")
                or parsed.get("job_postings")
                or next((v for v in parsed.values() if isinstance(v, list)), None)
            )
            if top_jobs is None:
                logger.warning("Gemini returned a dict without a list. Using fallback.")
                return jobs_batch[:9]
        elif isinstance(parsed, list):
            top_jobs = parsed
        else:
            logger.warning("Unexpected Gemini response format. Using fallback.")
            return jobs_batch[:9]

        # Validate essential fields
        valid_jobs = []
        for job in top_jobs:
            if isinstance(job, dict) and job.get("title") and job.get("link"):
                valid_jobs.append(job)

        if not valid_jobs:
            logger.warning("No valid job objects found in Gemini output. Using fallback.")
            return jobs_batch[:9]

        logger.info(f"Gemini successfully evaluated and returned {len(valid_jobs)} ranked jobs.")
        return valid_jobs[:9]

    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = response.json().get("error", {}).get("message", "")
        except Exception:
            error_detail = str(e)
        logger.error(f"Gemini API HTTP Error: {e} — Details: {error_detail}")
        return jobs_batch[:9]
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode Gemini JSON response: {e}")
        return jobs_batch[:9]
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return jobs_batch[:9]
