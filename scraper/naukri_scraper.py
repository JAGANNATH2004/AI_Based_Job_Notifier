from playwright.sync_api import sync_playwright
import logging
import time
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_CITIES = ["Hyderabad", "Bangalore", "Chennai"]

DEFAULT_CITY_URLS = [
    {
        "city": "Hyderabad",
        "url": "https://www.naukri.com/ai-ml-data-science-fresher-jobs-in-hyderabad?experience=0&experience=1&sort=r"
    },
    {
        "city": "Bangalore",
        "url": "https://www.naukri.com/ai-ml-data-science-fresher-jobs-in-bangalore?experience=0&experience=1&sort=r"
    },
    {
        "city": "Chennai",
        "url": "https://www.naukri.com/ai-ml-data-science-fresher-jobs-in-chennai?experience=0&experience=1&sort=r"
    },
]

# City name -> Naukri URL slug mapping
CITY_SLUG_MAP = {
    "Hyderabad": "hyderabad",
    "Bangalore":  "bangalore",
    "Chennai":    "chennai",
    "Delhi":      "delhi",
    "Mumbai":     "mumbai",
    "Pune":       "pune",
    "Kolkata":    "kolkata",
    "Noida":      "noida",
    "Gurgaon":    "gurgaon",
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
}


def _keyword_to_slug(keyword: str) -> str:
    """Convert a keyword string to a Naukri URL slug (e.g. 'Data Scientist' -> 'data-scientist')."""
    return keyword.lower().strip().replace(" ", "-").replace("/", "-")


def _normalize_city_name(city: str) -> str:
    """Extract base city name from full location strings like 'Hyderabad, Telangana, India'."""
    return city.split(",")[0].strip()


def _build_city_urls(keywords: list = None, cities: list = None) -> list:
    """
    Build Naukri search URLs from keyword list and city list.
    If no keywords are provided, returns the default hardcoded URLs.
    Iterates through up to 3 keyword slugs × all cities to cover multiple roles.
    """
    if not keywords:
        return DEFAULT_CITY_URLS

    target_cities = cities if cities else DEFAULT_CITIES

    # Use up to 3 keywords/roles to build URLs (each role × each city)
    role_slugs = []
    for kw in keywords[:3]:
        slug = _keyword_to_slug(kw)
        if slug and slug not in role_slugs:
            role_slugs.append(slug)

    if not role_slugs:
        role_slugs = ["data-science"]

    urls = []
    seen = set()
    for role_slug in role_slugs:
        for city in target_cities:
            base_city = _normalize_city_name(city)
            city_slug = CITY_SLUG_MAP.get(base_city, base_city.lower().replace(" ", "-"))
            url_key = f"{role_slug}-{city_slug}"
            if url_key in seen:
                continue
            seen.add(url_key)
            urls.append({
                "city": base_city,
                "url": (
                    f"https://www.naukri.com/{role_slug}-jobs-in-{city_slug}"
                    f"?experience=0&experience=1&sort=r"
                )
            })
    return urls


def _scrape_city(page, city: str, url: str) -> list:
    """Scrape one Naukri city URL and return list of job dicts using Playwright."""
    jobs = []
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        
        # Trigger some lazy loading / avoid bot detection
        page.wait_for_timeout(2000)
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            page.wait_for_timeout(1000)

        # Wait for either new or old class
        try:
            page.wait_for_selector('.srp-jobtuple-wrapper, .jobTuple', timeout=10000)
        except Exception:
            logger.warning(f"No Naukri job cards found for city: {city}")
            return jobs

        articles = page.query_selector_all('.srp-jobtuple-wrapper')
        if not articles:
            articles = page.query_selector_all('.jobTuple')

        logger.info(f"Naukri [{city}]: {len(articles)} job cards found.")

        for index, article in enumerate(articles):
            if index >= 20:
                break
            try:
                title_tag    = article.query_selector('a.title')
                company_tag  = article.query_selector('a.comp-name')
                location_tag = article.query_selector('span.locWdth')
                exp_tag      = article.query_selector('span.expwdth')
                posted_tag   = article.query_selector('span.job-post-day')

                if not title_tag:
                    continue

                title       = title_tag.inner_text().strip()
                link        = title_tag.get_attribute('href')
                if link:
                    link    = link.split('?')[0]
                company     = company_tag.inner_text().strip()  if company_tag  else ''
                location    = location_tag.inner_text().strip() if location_tag else city
                experience  = exp_tag.inner_text().strip()      if exp_tag      else '0-2 Yrs'
                posted_time = posted_tag.inner_text().strip()   if posted_tag   else 'Recently'

                if title and link:
                    jobs.append({
                        "title":       title,
                        "company":     company,
                        "location":    location,
                        "experience":  experience,
                        "posted_time": posted_time,
                        "link":        link,
                        "source":      "Naukri",
                        "description": "Details available via link"
                    })

            except Exception as e:
                logger.debug(f"Error parsing Naukri card ({city}): {e}")

    except Exception as e:
        logger.error(f"Naukri scrape failed for city '{city}': {e}")

    return jobs


def scrape_naukri_jobs(keywords: list = None, cities: list = None) -> list:
    """
    Scrape Naukri for AI/ML fresher jobs (0-2 years experience).

    Args:
        keywords: List of target job roles or search keyword strings.
                  Uses up to 3 keywords to build URL slugs. Falls back to
                  default AI/ML URLs when None.
        cities:   List of city name strings (e.g. ["Hyderabad", "Bangalore"]).
                  Falls back to Hyderabad, Bangalore, Chennai when None.

    Returns:
        List of job dicts.
    """
    city_urls = _build_city_urls(keywords, cities)
    all_jobs = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled']
            )
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                )
            )
            page = context.new_page()

            for entry in city_urls:
                city_jobs = _scrape_city(page, entry["city"], entry["url"])
                all_jobs.extend(city_jobs)
                logger.info(f"Naukri [{entry['city']}]: collected {len(city_jobs)} jobs.")

            browser.close()
    except Exception as e:
        logger.error(f"Playwright fatal error on Naukri: {e}")

    logger.info(f"Naukri total: {len(all_jobs)} jobs across all cities.")
    return all_jobs