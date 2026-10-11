from playwright.sync_api import sync_playwright
import time
import urllib.parse
import logging
from datetime import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logger = logging.getLogger(__name__)

# ── Defaults (used when no specific roles are configured) ─────────────────────
DEFAULT_KEYWORDS = (
    "Data Scientist OR Machine Learning Engineer OR AI Engineer "
    "OR Data Analyst OR NLP Engineer"
)

DEFAULT_CITIES = [
    "Hyderabad, Telangana, India",
    "Bengaluru, Karnataka, India",
    "Chennai, Tamil Nadu, India",
]


def _scrape_city(page, city: str, keywords: str) -> list:
    """Scrape LinkedIn jobs for a specific city and return list of job dicts."""
    jobs = []
    # f_E=2 → Entry Level | f_TPR=r86400 → Past 24 hours
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?f_E=2&f_TPR=r86400"
        f"&keywords={urllib.parse.quote(keywords)}"
        f"&location={urllib.parse.quote(city)}"
        f"&position=1&pageNum=0"
    )

    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)

        # Scroll to trigger lazy-loading
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

        try:
            page.wait_for_selector('.base-card', timeout=7000)
        except Exception:
            logger.warning(f"No LinkedIn job cards found for city: {city}")
            return jobs

        cards = page.query_selector_all('.base-card')
        logger.info(f"LinkedIn [{city}]: {len(cards)} cards found.")

        for index, card in enumerate(cards):
            if index >= 20:   # 20 per city × 3 cities = up to 60 total
                break
            try:
                title_elem    = card.query_selector('.base-search-card__title')
                company_elem  = card.query_selector('.base-search-card__subtitle')
                location_elem = card.query_selector('.job-search-card__location')
                link_elem     = card.query_selector('a.base-card__full-link')
                time_elem     = card.query_selector('.job-search-card__listdate--new')

                title    = title_elem.inner_text().strip()    if title_elem    else ""
                company  = company_elem.inner_text().strip()  if company_elem  else ""
                location = location_elem.inner_text().strip() if location_elem else city
                link     = link_elem.get_attribute('href').strip() if link_elem else ""
                link     = link.split('?')[0] if link else ""
                posted   = time_elem.inner_text().strip() if time_elem else datetime.now().strftime("%Y-%m-%d")

                if title and link:
                    jobs.append({
                        "title":       title,
                        "company":     company,
                        "location":    location,
                        "experience":  "0-1 year (Fresher/Entry Level)",
                        "posted_time": posted,
                        "link":        link,
                        "source":      "LinkedIn",
                        "description": "Details available via link"
                    })
            except Exception as e:
                logger.debug(f"Error parsing LinkedIn card ({city}): {e}")

    except Exception as e:
        logger.error(f"LinkedIn scrape failed for city '{city}': {e}")

    return jobs


def scrape_linkedin_jobs(keywords: str = None, cities: list = None) -> list:
    """
    Scrape LinkedIn for entry-level jobs posted in the past 24h.

    Args:
        keywords: Search keywords string (e.g. "Data Scientist OR ML Engineer").
                  Defaults to the built-in AI/ML fresher keyword set.
        cities:   List of LinkedIn-compatible location strings.
                  Defaults to Hyderabad, Bengaluru, Chennai.

    Returns:
        List of job dicts.
    """
    search_keywords = keywords if keywords else DEFAULT_KEYWORDS
    search_cities   = cities   if cities   else DEFAULT_CITIES

    all_jobs = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                )
            )
            page = context.new_page()

            for city in search_cities:
                city_jobs = _scrape_city(page, city, search_keywords)
                all_jobs.extend(city_jobs)
                logger.info(f"LinkedIn [{city}]: collected {len(city_jobs)} jobs.")

            browser.close()

    except Exception as e:
        logger.error(f"Playwright fatal error: {e}")

    logger.info(f"LinkedIn total: {len(all_jobs)} jobs across all cities.")
    return all_jobs
