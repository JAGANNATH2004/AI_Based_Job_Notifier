"""
main.py
AI Job Scraper & Ranker Pipeline — Master Entry Point

Features:
  - Scrapes LinkedIn & Naukri for user-configurable roles and locations
  - Pre-filters jobs by link, title, target cities, and experience (0-1 yr exp)
  - Deduplicates jobs against Supabase Cloud PostgreSQL (scraped_jobs)
  - Ranks top opportunities using Google Gemini AI
  - Dispatches curated daily digest directly to Telegram
  - Automates scheduling via APScheduler (periodic scrape + daily digest at user-specified time)
  - Optional CLI modes: --now (immediate run), --web (FastAPI web UI), --both (web + scheduler)
"""
import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime
import pytz

# Ensure utf-8 output on Windows consoles
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add current directory to path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    JOB_LOCATIONS,
    JOB_ROLES,
    REPORT_TIME,
    REPORT_TIMEZONE,
    SCRAPE_INTERVAL_HOURS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from database.db import get_total_jobs_count, init_db
from processor.deduplicator import deduplicate_and_store_jobs
from processor.filter_jobs import filter_basic_jobs
from processor.ranker import get_top_jobs
from scraper.linkedin_scraper import scrape_linkedin_jobs
from scraper.naukri_scraper import scrape_naukri_jobs
from notifier.telegram_notifier import notify_top_jobs

logger = logging.getLogger("ai_job_pipeline")


def print_banner(locations=None, roles=None, report_time=None):
    """Display startup banner with active configuration summary."""
    telegram_configured = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    gemini_configured = bool(GEMINI_API_KEY)
    ai_status = f"Google Gemini ({GEMINI_MODEL}) {'✅' if gemini_configured else '⚠️ (Set GEMINI_API_KEY in .env)'}"

    active_locs = locations or JOB_LOCATIONS
    active_roles = roles or JOB_ROLES
    active_time = report_time or REPORT_TIME

    from database.supabase_client import is_supabase_configured
    supabase_configured = is_supabase_configured()
    users_info = "Multi-User Cloud (Supabase)" if supabase_configured else "Local Single-User"

    if supabase_configured and (not active_locs or not active_roles):
        try:
            from database.user_service import get_all_active_scheduled_users
            users = get_all_active_scheduled_users()
            if users:
                users_info = f"{len(users)} active user(s) in Supabase"
                if not active_roles:
                    all_roles = set()
                    for u in users:
                        all_roles.update(u.get("roles") or [])
                    active_roles = list(all_roles)
                if not active_locs:
                    all_locs = set()
                    for u in users:
                        all_locs.update(u.get("locations") or [])
                    active_locs = list(all_locs)
                telegram_configured = any(u.get("bot_token") and u.get("chat_id") for u in users)
        except Exception:
            pass

    roles_str = ", ".join(active_roles) if active_roles else "Dynamic (Extracted per user from database)"
    locs_str = ", ".join(active_locs) if active_locs else "Dynamic (Extracted per user from database)"

    print("\n" + "═" * 65)
    print("        🚀 AI Job Scraper & Ranker Pipeline 🚀")
    print("═" * 65)
    print(f" • Mode          : {users_info}")
    print(f" • Target Roles  : {roles_str}")
    print(f" • Locations     : {locs_str}")
    print(" • Database      : Supabase Cloud PostgreSQL ✅")
    print(f" • AI Engine     : {ai_status}")
    print(f" • Telegram Bot  : {'Configured per user in Supabase ✅' if telegram_configured else 'Stored per user in database ⚠️'}")
    print(f" • Daily Report  : Extracted per user ({REPORT_TIMEZONE})")
    print(f" • Scrape Interval: Every {SCRAPE_INTERVAL_HOURS} hours")
    print("═" * 65 + "\n")


def run_scraping_cycle(target_cities: list = None, target_roles: list = None, user_id: str = None) -> list:
    """
    Executes a single scraping cycle:
      1. Extracts target roles & locations directly from active users in Supabase (or CLI arguments)
      2. Scrapes LinkedIn & Naukri across these target locations & roles
      3. Pre-filters for 0-1 yrs exp, target cities, and roles
      4. Deduplicates against database and stores new listings in Supabase
      5. Returns fresh jobs found in this cycle
    """
    cities = target_cities
    roles = target_roles

    # If specific user_id is passed, extract their exact locations and roles from Supabase
    if user_id:
        from database.user_service import get_user_by_id
        u = get_user_by_id(user_id)
        if u:
            cities = u.get("locations") or []
            roles = u.get("roles") or []
            logger.info(f"Extracted preferences for user {user_id}: locations={cities}, roles={roles}")

    # Extract target locations & roles directly from user database if not passed explicitly
    if not cities or not roles:
        from database.supabase_client import is_supabase_configured
        if is_supabase_configured():
            try:
                from database.user_service import get_all_active_scheduled_users
                active_users = get_all_active_scheduled_users()
                cities_set = set(cities) if cities else set()
                roles_set = set(roles) if roles else set()
                for u in active_users:
                    for loc in (u.get("locations") or []):
                        if loc and str(loc).strip():
                            cities_set.add(str(loc).strip())
                    for r in (u.get("roles") or []):
                        if r and str(r).strip():
                            roles_set.add(str(r).strip())
                if cities_set:
                    cities = list(cities_set)
                if roles_set:
                    roles = list(roles_set)
            except Exception as e:
                logger.warning(f"Could not extract user preferences from database: {e}")

    cities = cities or JOB_LOCATIONS
    roles = roles or JOB_ROLES

    if not cities or not roles:
        logger.warning(
            "Target locations or job roles have not been provided yet. "
            "Please configure them in the web interface (http://127.0.0.1:8000) or via --locations and --roles CLI flags."
        )
        return []

    logger.info(f"Starting job scraping cycle for roles: {roles} in {cities}...")
    scraped_jobs = []

    # 1. Scrape LinkedIn
    try:
        linkedin_keywords = " OR ".join(roles[:5]) if roles else None
        logger.info(f"Scraping LinkedIn with keywords: '{linkedin_keywords}' in {cities}...")
        li_jobs = scrape_linkedin_jobs(keywords=linkedin_keywords, cities=cities)
        logger.info(f"LinkedIn returned {len(li_jobs)} total listings.")
        scraped_jobs.extend(li_jobs)
    except Exception as e:
        logger.error(f"Error during LinkedIn scraping: {e}")

    # 2. Scrape Naukri
    try:
        logger.info(f"Scraping Naukri with roles: {roles} in {cities}...")
        nk_jobs = scrape_naukri_jobs(keywords=roles, cities=cities)
        logger.info(f"Naukri returned {len(nk_jobs)} total listings.")
        scraped_jobs.extend(nk_jobs)
    except Exception as e:
        logger.error(f"Error during Naukri scraping: {e}")

    if not scraped_jobs:
        logger.warning("No jobs were scraped from either platform.")
        return []

    logger.info(f"Total raw scraped jobs collected: {len(scraped_jobs)}")

    # 3. Apply 4-stage basic filtering with user-specified locations & roles
    filtered_jobs = filter_basic_jobs(scraped_jobs, target_cities=cities, job_roles=roles)
    logger.info(f"Jobs remaining after pre-filtering: {len(filtered_jobs)}")

    # 4. Deduplicate and persist fresh jobs directly to Supabase cloud
    fresh_jobs = deduplicate_and_store_jobs(filtered_jobs, user_id=user_id)
    logger.info(f"New fresh jobs found in this run: {len(fresh_jobs)}")

    return fresh_jobs


def dispatch_for_user(user: dict, available_jobs: list = None, dry_run: bool = False, is_scheduled: bool = False):
    """
    Evaluates jobs matching a specific user's preferences, deduplicates against their
    delivered history in Supabase, ranks with Gemini AI, and sends to their personal Telegram bot.
    When is_scheduled=True, ensures the daily digest is reliably delivered even if candidates were previewed.
    """
    uid = user["user_id"]
    bot_token = user.get("bot_token")
    chat_id = user.get("chat_id")
    user_locs = user.get("locations") or []
    user_roles = user.get("roles") or []
    user_exp = user.get("experience_level", "fresher")

    from database.user_service import (
        get_user_delivered_links,
        record_delivered_jobs,
        update_user_last_dispatched,
        get_recent_scraped_jobs_from_supabase,
    )

    delivered_links = get_user_delivered_links(uid)
    jobs_pool = list(available_jobs) if available_jobs else []

    # If no jobs in cache, fetch recent jobs from Supabase
    if not jobs_pool:
        jobs_pool = get_recent_scraped_jobs_from_supabase(limit=100)

    # If still no jobs in database, scrape immediately for this user's locations and roles
    if not jobs_pool and (user_locs or user_roles):
        logger.info(f"User {uid}: No jobs in pool, running immediate scrape for locations={user_locs}, roles={user_roles}...")
        jobs_pool = run_scraping_cycle(target_cities=user_locs, target_roles=user_roles, user_id=uid)

    fresh_for_user = [j for j in jobs_pool if j.get("link") not in delivered_links]

    matched_jobs = fresh_for_user
    if user_locs or user_roles or user_exp:
        matched_jobs = filter_basic_jobs(fresh_for_user, target_cities=user_locs, job_roles=user_roles, experience_level=user_exp)

    # If existing pool didn't have matches, trigger a targeted scrape for this user's preferences
    if not matched_jobs and (user_locs or user_roles) and available_jobs is None:
        logger.info(f"User {uid}: No matching unread jobs in pool. Running targeted scrape for locations={user_locs}, roles={user_roles}...")
        fresh_scraped = run_scraping_cycle(target_cities=user_locs, target_roles=user_roles, user_id=uid)
        fresh_for_user = [j for j in fresh_scraped if j.get("link") not in delivered_links]
        if user_locs or user_roles or user_exp:
            matched_jobs = filter_basic_jobs(fresh_for_user, target_cities=user_locs, job_roles=user_roles, experience_level=user_exp)

    # At scheduled dispatch time, ensure the user ALWAYS receives their daily digest
    if not matched_jobs and is_scheduled:
        logger.info(f"User {uid}: Scheduled digest fallback — curating top matching jobs from existing database pool...")
        all_pool = list(jobs_pool)
        if not all_pool:
            all_pool = get_recent_scraped_jobs_from_supabase(limit=100)
        if not all_pool and (user_locs or user_roles):
            all_pool = run_scraping_cycle(target_cities=user_locs, target_roles=user_roles, user_id=uid)
        if all_pool and (user_locs or user_roles or user_exp):
            matched_jobs = filter_basic_jobs(all_pool, target_cities=user_locs, job_roles=user_roles, experience_level=user_exp)

    if not matched_jobs:
        logger.info(f"User {uid}: No jobs matching preferences (locations: {user_locs}, roles: {user_roles}) to dispatch.")
        return 0

    profile = {
        "roles": user_roles,
        "experience_level": user_exp,
    }
    try:
        top_jobs = get_top_jobs(matched_jobs, profile=profile)
    except Exception as e:
        logger.error(f"User {uid}: Gemini ranking error: {e}")
        top_jobs = matched_jobs[:9]

    if dry_run:
        logger.info(f"[Dry Run] User {uid}: Found {len(top_jobs)} top jobs.")
        return len(top_jobs)
    else:
        if not bot_token or not chat_id:
            logger.warning(f"User {uid}: Skipping Telegram dispatch — bot token or chat ID is not configured.")
            return 0

        success = notify_top_jobs(top_jobs, bot_token=bot_token, chat_id=chat_id, profile=profile)
        if success:
            record_delivered_jobs(uid, top_jobs)
            update_user_last_dispatched(uid)
            logger.info(f"User {uid}: Successfully dispatched {len(top_jobs)} jobs to Telegram.")
            return len(top_jobs)
        else:
            logger.warning(f"User {uid}: Failed to deliver Telegram message.")
            return 0


def run_daily_ranking_and_dispatch(dry_run: bool = False, profile: dict = None):
    """
    Ranks fresh jobs from Supabase and dispatches them via Telegram.
    Supports multi-tenant Supabase users or single-user fallback.
    Queries Supabase directly instead of relying on a shared in-memory cache.
    """
    logger.info("Starting daily evaluation and digest dispatch...")

    # Check for multi-tenant Supabase active users
    from database.supabase_client import is_supabase_configured
    if is_supabase_configured():
        from database.user_service import get_all_active_scheduled_users, get_recent_scraped_jobs_from_supabase
        active_users = get_all_active_scheduled_users()
        if active_users:
            # Fetch recent jobs from Supabase to use as the shared pool
            jobs_pool = get_recent_scraped_jobs_from_supabase(limit=200)
            logger.info(f"Multi-user mode: dispatching daily digests for {len(active_users)} active Supabase users ({len(jobs_pool)} jobs in pool)...")
            for u in active_users:
                try:
                    dispatch_for_user(u, jobs_pool, dry_run=dry_run)
                except Exception as ex:
                    logger.error(f"Error dispatching for user {u.get('user_id')}: {ex}")
            return

    # Single-user fallback — fetch from Supabase
    from database.user_service import get_recent_scraped_jobs_from_supabase
    jobs_to_process = get_recent_scraped_jobs_from_supabase(limit=100)

    if not jobs_to_process:
        logger.info("Daily queue is empty. No fresh jobs to rank or dispatch today.")
        return

    try:
        logger.info(f"Passing {len(jobs_to_process)} jobs to Google Gemini ({GEMINI_MODEL}) for ranking...")
        top_jobs = get_top_jobs(jobs_to_process, profile=profile)
    except Exception as e:
        logger.error(f"Failed to rank jobs via AI: {e}. Falling back to top 9 unranked.")
        top_jobs = jobs_to_process[:9]

    logger.info(f"Selected top {len(top_jobs)} jobs for today's digest.")

    # Output to console
    print("\n" + "─" * 60)
    print(f"🌟 TOP {len(top_jobs)} CURATED JOBS FOR TODAY:")
    print("─" * 60)
    for idx, j in enumerate(top_jobs, 1):
        print(f"{idx}. {j.get('title')} | {j.get('company')} | {j.get('location')}")
        print(f"   Link: {j.get('link')}")
    print("─" * 60 + "\n")

    # Send to Telegram
    if dry_run:
        logger.info("[Dry Run] Skipping Telegram message transmission.")
    else:
        logger.info("Sending job digest to Telegram...")
        success = notify_top_jobs(top_jobs)
        if success:
            logger.info("Telegram notification successfully delivered!")
        else:
            logger.warning("Could not deliver Telegram notification. Check bot token and chat ID.")


def run_immediate_cycle(target_cities: list = None, target_roles: list = None, dry_run: bool = False):
    """Run one complete cycle right now: Scrape -> Filter -> Deduplicate -> Gemini Rank -> Telegram."""
    logger.info("Running immediate full cycle (scrape + rank + notify)...")
    run_scraping_cycle(target_cities=target_cities, target_roles=target_roles)
    run_daily_ranking_and_dispatch(dry_run=dry_run)


def setup_scheduler_jobs(
    scheduler,
    dry_run: bool = False,
    target_cities: list = None,
    target_roles: list = None,
    report_time: str = None
):
    """Register all recurring jobs: scraping cycle, per-user schedule monitor, and fallback digest."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = pytz.timezone(REPORT_TIMEZONE)
    active_time = report_time or REPORT_TIME
    try:
        report_hour, report_minute = map(int, active_time.split(":"))
    except ValueError:
        report_hour, report_minute = 9, 41

    # Job 1: Regular scraping cycle — dynamically reads all users' preferences at runtime
    def _dynamic_scrape():
        """Dynamically aggregate all active users' preferences and scrape."""
        run_scraping_cycle(target_cities=target_cities, target_roles=target_roles)

    scheduler.add_job(
        _dynamic_scrape,
        trigger=IntervalTrigger(hours=SCRAPE_INTERVAL_HOURS, timezone=tz),
        id="periodic_job_scraper",
        name="Periodic Job Scraper",
        replace_existing=True,
    )

    # Job 2: Multi-User minute-by-minute schedule monitor (dispatches at each user's exact report_time)
    def check_user_schedules_and_dispatch():
        """Checks if current time in user's timezone matches their database report_time."""
        from database.supabase_client import is_supabase_configured
        if not is_supabase_configured():
            return

        from database.user_service import get_all_active_scheduled_users
        try:
            active_users = get_all_active_scheduled_users()
            if not active_users:
                return

            now_utc = datetime.now(pytz.utc)

            for u in active_users:
                user_tz_str = u.get("timezone", REPORT_TIMEZONE)
                try:
                    user_tz = pytz.timezone(user_tz_str)
                except Exception:
                    user_tz = pytz.timezone("Asia/Kolkata")

                user_now = now_utc.astimezone(user_tz)
                user_hhmm = user_now.strftime("%H:%M")
                target_time = u.get("report_time", "09:41")

                if user_hhmm == target_time:
                    # Check if already dispatched for this specific scheduled time today
                    last_disp = u.get("last_dispatched_at")
                    already_dispatched = False
                    if last_disp:
                        try:
                            last_disp_dt = datetime.fromisoformat(last_disp.replace("Z", "+00:00")).astimezone(user_tz)
                            # Only consider already dispatched if this exact schedule (same date and same HH:MM) was already sent
                            if last_disp_dt.date() == user_now.date() and last_disp_dt.strftime("%H:%M") == target_time:
                                already_dispatched = True
                        except Exception:
                            pass

                    if not already_dispatched:
                        logger.info(
                            f"User {u['user_id']} report time reached ({user_hhmm} {user_tz_str}). "
                            f"Preferences: roles={u.get('roles')}, locations={u.get('locations')}. "
                            f"Dispatching scheduled jobs..."
                        )
                        dispatch_for_user(u, dry_run=dry_run, is_scheduled=True)
        except Exception as ex:
            logger.error(f"Error checking user schedules: {ex}")

    scheduler.add_job(
        check_user_schedules_and_dispatch,
        trigger=CronTrigger(second=0, timezone=tz),
        id="multi_user_schedule_monitor",
        name="Multi-User Schedule Monitor",
        replace_existing=True,
    )

    # Job 3: Fallback daily dispatch for CLI single-user runs
    scheduler.add_job(
        lambda: run_daily_ranking_and_dispatch(dry_run=dry_run),
        trigger=CronTrigger(hour=report_hour, minute=report_minute, timezone=tz),
        id="daily_telegram_report",
        name="Daily Telegram Digest",
        replace_existing=True,
    )


def start_scheduler(
    skip_startup_scrape: bool = False,
    dry_run: bool = False,
    target_cities: list = None,
    target_roles: list = None,
    report_time: str = None
):
    """
    Initializes and starts the APScheduler background scheduler in blocking mode.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    tz = pytz.timezone(REPORT_TIMEZONE)
    scheduler = BlockingScheduler(timezone=tz)

    setup_scheduler_jobs(
        scheduler,
        dry_run=dry_run,
        target_cities=target_cities,
        target_roles=target_roles,
        report_time=report_time,
    )

    logger.info(
        f"Scheduler active: Multi-user database monitor running every minute | "
        f"Scraping every {SCRAPE_INTERVAL_HOURS}h."
    )

    # Run immediate startup scrape cycle if requested
    if not skip_startup_scrape:
        logger.info("Executing initial startup scraping cycle...")
        try:
            run_scraping_cycle(target_cities=target_cities, target_roles=target_roles)
        except Exception as e:
            logger.error(f"Initial startup scrape encountered an error: {e}")

    print("\nPipeline is running in background. Press Ctrl+C to exit.\n")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Pipeline scheduler stopped gracefully.")


def start_web_server(host: str = "127.0.0.1", port: int = 8000):
    """Start the FastAPI Web UI server via uvicorn."""
    import uvicorn
    from web_app import app

    print(f"\n🌐 Starting AI-JobScout Web Interface at http://{host}:{port}")
    print(f"📖 API Documentation at http://{host}:{port}/docs\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    parser = argparse.ArgumentParser(
        description="AI Job Scraper & Ranker Pipeline — Playwright Scraper, Gemini AI Ranker & Telegram Bot"
    )
    parser.add_argument(
        "--locations",
        type=str,
        default=None,
        help="Comma-separated job locations (e.g. 'Hyderabad, Bangalore, Pune')."
    )
    parser.add_argument(
        "--roles",
        type=str,
        default=None,
        help="Comma-separated target roles (e.g. 'Data Scientist, AI Engineer, Python Developer')."
    )
    parser.add_argument(
        "--time",
        type=str,
        default=None,
        help="Daily report time in HH:MM format (e.g. '09:41' or '19:00')."
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run an immediate full scrape, AI ranking, and Telegram dispatch cycle, then exit."
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the FastAPI web interface with settings manager and live search."
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Run both the background scheduler pipeline and the FastAPI web server concurrently."
    )
    parser.add_argument(
        "--skip-startup-scrape",
        action="store_true",
        help="Start the scheduler without running an immediate scrape on startup."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and rank jobs, but do not send messages to Telegram."
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address for the web server (default: 127.0.0.1)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the web server (default: 8000)."
    )

    args = parser.parse_args()

    # Parse CLI overrides if provided
    cli_cities = [c.strip() for c in args.locations.split(",") if c.strip()] if args.locations else None
    cli_roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    cli_time = args.time.strip() if args.time else None

    print_banner(locations=cli_cities, roles=cli_roles, report_time=cli_time)

    # 1. Initialize SQLite Database
    init_db()
    existing_count = get_total_jobs_count()
    logger.info(f"Database ready: currently tracking {existing_count} previously saved jobs.")

    # 2. Handle Execution Modes
    if args.web:
        start_web_server(host=args.host, port=args.port)

    elif args.both:
        from apscheduler.schedulers.background import BackgroundScheduler
        from contextlib import asynccontextmanager
        import uvicorn

        tz = pytz.timezone(REPORT_TIMEZONE)
        bg_scheduler = BackgroundScheduler(timezone=tz)
        setup_scheduler_jobs(
            bg_scheduler,
            dry_run=args.dry_run,
            target_cities=cli_cities,
            target_roles=cli_roles,
            report_time=cli_time,
        )

        # Use FastAPI lifespan for clean scheduler lifecycle
        from web_app import app

        @asynccontextmanager
        async def lifespan(app):
            # Startup
            bg_scheduler.start()
            logger.info(f"Background scheduler started: Multi-user monitor + periodic scraper every {SCRAPE_INTERVAL_HOURS}h.")

            if not args.skip_startup_scrape:
                threading.Thread(
                    target=lambda: run_scraping_cycle(target_cities=cli_cities, target_roles=cli_roles),
                    name="initial-scrape",
                    daemon=True,
                ).start()

            yield  # App is running

            # Shutdown
            logger.info("Shutting down background scheduler...")
            bg_scheduler.shutdown(wait=False)
            logger.info("Background scheduler stopped.")

        app.router.lifespan_context = lifespan

        print(f"\n🌐 Starting AI-JobScout Web Interface at http://{args.host}:{args.port}")
        print(f"📖 API Documentation at http://{args.host}:{args.port}/docs\n")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")

    elif args.now:
        run_immediate_cycle(target_cities=cli_cities, target_roles=cli_roles, dry_run=args.dry_run)

    else:
        start_scheduler(
            skip_startup_scrape=args.skip_startup_scrape,
            dry_run=args.dry_run,
            target_cities=cli_cities,
            target_roles=cli_roles,
            report_time=cli_time,
        )


if __name__ == "__main__":
    main()
