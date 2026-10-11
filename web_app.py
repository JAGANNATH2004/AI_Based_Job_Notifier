"""
web_app.py
FastAPI web application & dashboard for AI Job Scraper & Ranker.

Features:
  - Interactive Job Search: customize target roles & locations with live AI ranking
  - Real-time SSE streaming for LinkedIn/Naukri scraping & Gemini AI ranking
  - Settings Management: configure job locations, target roles, Telegram bot token/chat ID,
    daily scheduler time, and Gemini API key directly from the UI
  - Telegram integration testing directly from the browser
  - Manual pipeline trigger on demand
  - Database viewer for saved jobs
"""
import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import html
import config
from config import save_settings_to_env
from database.db import get_recent_jobs, get_total_jobs_count, init_db
from notifier.telegram_notifier import send_telegram_message, notify_top_jobs, send_settings_update_notification
from processor.filter_jobs import filter_basic_jobs
from processor.ranker import get_top_jobs
from scraper.linkedin_scraper import scrape_linkedin_jobs
from scraper.naukri_scraper import scrape_naukri_jobs
from auth.auth_handler import RegisterRequest, LoginRequest, register_user, login_user, get_current_user, decode_user_token
from database.supabase_client import is_supabase_configured, get_supabase_client
from database.user_service import get_all_user_settings, save_all_user_settings, get_user_by_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# Ensure database is initialized on startup
init_db()

app = FastAPI(
    title="AI-JobScout & Pipeline Dashboard",
    description="Configure job search locations, roles, Telegram notifications, and AI ranking.",
    version="3.0.0",
)

# ── CORS Middleware ──────────────────────────────────────────────────────────
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Security Headers Middleware ──────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ── In-Memory Rate Limiter for Auth Endpoints ────────────────────────────────
import time as _time
from collections import defaultdict

_rate_limit_store: dict = defaultdict(list)  # ip -> [timestamp, ...]
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 10  # max login/register attempts per window per IP


def _check_rate_limit(client_ip: str) -> bool:
    """Returns True if the request should be BLOCKED (rate limit exceeded)."""
    now = _time.time()
    # Prune old entries
    _rate_limit_store[client_ip] = [
        ts for ts in _rate_limit_store[client_ip] if now - ts < RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    _rate_limit_store[client_ip].append(now)
    return False


# Mount static files (CSS, JS assets)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Global pipeline run lock
pipeline_lock = threading.Lock()
pipeline_status = {"running": False, "last_run": None, "message": "Idle"}

# Concurrency semaphore for Playwright browser scraping (max 2 simultaneous)
import asyncio

_scraper_semaphore = asyncio.Semaphore(2)


# ─────────────────────────────────────────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main application & configuration dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")


# ─────────────────────────────────────────────────────────────────────────────
# Live SSE Search Endpoint (With Input Locations & Roles)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/find-jobs")
async def find_jobs_endpoint(
    skills:           str = Query(default=""),
    keywords:         str = Query(default=""),
    experience_level: str = Query(default="fresher"),
    roles:            str = Query(default=""),
    locations:        str = Query(default=""),
    token:            str = Query(default=""),
):
    """
    Server-Sent Events stream that:
      1. Scrapes LinkedIn using user-specified locations & roles
      2. Scrapes Naukri using user-specified locations & roles
      3. Pre-filters results matching 0-1 yr exp & user criteria
      4. Ranks results using Google Gemini AI + candidate profile
      5. Streams real-time progress and final ranked jobs to client
    """
    skills_list = [s.strip() for s in skills.split(",") if s.strip()]
    keywords_list = [k.strip() for k in keywords.split(",") if k.strip()]
    roles_list = [r.strip() for r in roles.split(",") if r.strip()]
    locations_list = [loc.strip() for loc in locations.split(",") if loc.strip()]

    # If user is authenticated and did not explicitly override roles/locations, extract from Supabase
    if token and is_supabase_configured() and (not roles_list or not locations_list):
        try:
            u_info = decode_user_token(token)
            if u_info and u_info.get("id"):
                u_settings = get_all_user_settings(u_info["id"], access_token=token)
                if not roles_list and u_settings.get("job_roles"):
                    roles_list = u_settings["job_roles"]
                if not locations_list and u_settings.get("job_locations"):
                    locations_list = u_settings["job_locations"]
                if experience_level == "fresher" and u_settings.get("experience_level"):
                    experience_level = u_settings["experience_level"]
        except Exception as e:
            logger.warning(f"Could not load user preferences from Supabase: {e}")

    roles_list = roles_list or config.JOB_ROLES
    locations_list = locations_list or config.JOB_LOCATIONS

    profile = {
        "skills": skills_list,
        "keywords": keywords_list,
        "experience_level": experience_level,
        "roles": roles_list,
    }

    # Combined search keywords for LinkedIn
    combined_terms = roles_list + keywords_list
    linkedin_keywords = " OR ".join(combined_terms[:5]) if combined_terms else None
    naukri_keywords = roles_list if roles_list else keywords_list if keywords_list else None

    async def event_stream():
        loop = asyncio.get_event_loop()

        # Limit concurrent browser scraping to prevent memory exhaustion
        async with _scraper_semaphore:

            if not locations_list:
                yield _sse({"type": "error", "message": "No target locations provided. Please add at least one location from the interface."})
                return

            if not roles_list:
                yield _sse({"type": "error", "message": "No target job roles provided. Please add at least one role from the interface."})
                return

            # ── Step 1: LinkedIn ─────────────────────────────────────────────
            locs_str = ", ".join(locations_list)
            yield _sse({
                "type": "progress", "step": 1,
                "message": f"Searching LinkedIn across: {locs_str}..."
            })

            try:
                linkedin_jobs = await loop.run_in_executor(
                    None, lambda: scrape_linkedin_jobs(keywords=linkedin_keywords, cities=locations_list)
                )
                yield _sse({
                    "type": "progress", "step": 1,
                    "message": f"Found {len(linkedin_jobs)} jobs on LinkedIn"
                })
            except Exception as e:
                logger.error(f"LinkedIn search error: {e}")
                linkedin_jobs = []
                yield _sse({
                    "type": "progress", "step": 1,
                    "message": "LinkedIn search unavailable — continuing with Naukri"
                })

            # ── Step 2: Naukri ───────────────────────────────────────────────
            yield _sse({
                "type": "progress", "step": 2,
                "message": f"Searching Naukri for {len(roles_list)} target roles in {locs_str}..."
            })

            try:
                naukri_jobs = await loop.run_in_executor(
                    None, lambda: scrape_naukri_jobs(keywords=naukri_keywords, cities=locations_list)
                )
                yield _sse({
                    "type": "progress", "step": 2,
                    "message": f"Found {len(naukri_jobs)} jobs on Naukri"
                })
            except Exception as e:
                logger.error(f"Naukri search error: {e}")
                naukri_jobs = []
                yield _sse({
                    "type": "progress", "step": 2,
                    "message": "Naukri search unavailable — continuing with LinkedIn results"
                })

            all_jobs = linkedin_jobs + naukri_jobs

            if not all_jobs:
                yield _sse({
                    "type": "error",
                    "message": "No jobs found. Try adjusting locations or roles."
                })
                return

            # Pre-filter according to selected criteria
            filtered = filter_basic_jobs(all_jobs, target_cities=locations_list, job_roles=roles_list, experience_level=experience_level)
            jobs_to_rank = filtered if filtered else all_jobs

            # ── Step 3: Match and Rank ───────────────────────────────────────
            yield _sse({
                "type": "progress", "step": 3,
                "message": f"Ranking {len(jobs_to_rank)} matching jobs..."
            })

            try:
                top_jobs = await loop.run_in_executor(
                    None, lambda: get_top_jobs(jobs_to_rank, profile=profile)
                )
                yield _sse({
                    "type": "progress", "step": 3,
                    "message": f"Selected top {len(top_jobs)} jobs matched to your preferences"
                })
            except Exception as e:
                logger.error(f"Ranking error: {e}")
                top_jobs = jobs_to_rank[:9]
                yield _sse({
                    "type": "progress", "step": 3,
                    "message": "Displaying top matching jobs"
                })

            # ── Record delivered jobs in Supabase if user is logged in ───────
            if token and is_supabase_configured():
                try:
                    u_info = decode_user_token(token)
                    if u_info and u_info.get("id"):
                        from database.user_service import record_delivered_jobs
                        record_delivered_jobs(u_info["id"], top_jobs)
                except Exception as e:
                    logger.warning(f"Could not record delivered jobs in Supabase: {e}")

            # ── Emit final result ────────────────────────────────────────────
            yield _sse({"type": "result", "jobs": top_jobs})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Supabase Authentication APIs
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status():
    """Check if Supabase authentication backend is configured."""
    return {
        "supabase_configured": is_supabase_configured(),
        "supabase_url_set": bool(os.getenv("SUPABASE_URL")),
    }


@app.post("/api/auth/register")
async def api_register(req: RegisterRequest, request: Request):
    """Register a new user account via Supabase Auth."""
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(f"register:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many registration attempts. Please try again later.")
    return register_user(req)


@app.post("/api/auth/login")
async def api_login(req: LoginRequest, request: Request):
    """Authenticate user with email and password via Supabase Auth."""
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(f"login:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many login attempts. Please try again later.")
    return login_user(req)


@app.get("/api/auth/me")
async def api_me(user: dict = Depends(get_current_user)):
    """Retrieve active authenticated user details."""
    return {"user": user}


# ─────────────────────────────────────────────────────────────────────────────
# Settings Management APIs (Supabase + Local fallback)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(request: Request):
    """
    Retrieve configurable settings.
    If an Authorization Bearer token is provided, loads the user's settings from Supabase.
    Otherwise, falls back to local configuration.
    """
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    if token and is_supabase_configured():
        try:
            u_info = decode_user_token(token)
            if u_info and u_info.get("id"):
                uid = u_info["id"]
                user_settings = get_all_user_settings(uid, access_token=token)
                if user_settings:
                    return {
                        "job_locations": user_settings.get("job_locations", []),
                        "job_roles": user_settings.get("job_roles", []),
                        "experience_level": user_settings.get("experience_level", "fresher"),
                        "telegram_bot_token": user_settings.get("telegram_bot_token", ""),
                        "telegram_chat_id": user_settings.get("telegram_chat_id", ""),
                        "report_time": user_settings.get("report_time", "09:41"),
                        "report_timezone": user_settings.get("timezone", "Asia/Kolkata"),
                        "scrape_interval_hours": user_settings.get("scrape_interval_hours", 24),
                        "pipeline_status": pipeline_status,
                        "storage_backend": "supabase",
                        "user_id": uid,
                    }
        except Exception as e:
            logger.warning(f"Failed to load user settings from Supabase: {e}")

    # Fallback to local config
    return {
        "job_locations": config.JOB_LOCATIONS,
        "job_roles": config.JOB_ROLES,
        "telegram_bot_token": config.TELEGRAM_BOT_TOKEN,
        "telegram_chat_id": config.TELEGRAM_CHAT_ID,
        "report_time": config.REPORT_TIME,
        "report_timezone": config.REPORT_TIMEZONE,
        "scrape_interval_hours": config.SCRAPE_INTERVAL_HOURS,
        "pipeline_status": pipeline_status,
        "storage_backend": "local",
    }


def _auto_sync_jobs_for_user(user_obj: dict):
    """
    Background worker automatically triggered whenever a user updates their settings.
    Gathers and scrapes fresh listings matching the new locations and roles into Supabase,
    so that candidate jobs are primed and ready for the daily scheduler to dispatch at user's report_time.
    Does NOT prematurely blast out the daily digest immediately (which is reserved for the scheduled time).
    """
    from datetime import datetime
    from main import run_scraping_cycle

    pipeline_status["running"] = True
    uid = user_obj.get("user_id")
    locs = user_obj.get("locations") or []
    roles = user_obj.get("roles") or []
    exp = user_obj.get("experience_level", "fresher")
    r_time = user_obj.get("report_time", "09:41")

    roles_str = ", ".join(roles) if roles else "target roles"
    locs_str = ", ".join(locs) if locs else "target locations"
    pipeline_status["message"] = f"Pre-fetching jobs for {roles_str} in {locs_str}..."

    try:
        logger.info(
            f"⚡ Auto-sync initiated for user {uid}: roles={roles}, locations={locs}, exp={exp}"
        )
        fresh_jobs = run_scraping_cycle(target_cities=locs, target_roles=roles, user_id=uid)
        logger.info(
            f"⚡ Auto-sync primed {len(fresh_jobs)} fresh jobs into database for user {uid}. "
            f"Scheduled digest will deliver at {r_time}."
        )

        pipeline_status["message"] = f"Jobs primed for {roles_str}. Daily digest scheduled for {r_time}."
        pipeline_status["last_run"] = datetime.now().strftime("%H:%M")
    except Exception as e:
        logger.error(f"Error in _auto_sync_jobs_for_user: {e}", exc_info=True)
        pipeline_status["message"] = f"Auto-sync error: {e}"
    finally:
        pipeline_status["running"] = False


def _auto_sync_jobs_local(experience_level: str = "fresher"):
    """Background worker for local single-user mode when settings are updated."""
    from datetime import datetime
    from main import run_scraping_cycle

    pipeline_status["running"] = True
    pipeline_status["message"] = "Pre-fetching jobs based on updated .env settings..."
    try:
        fresh = run_scraping_cycle(target_cities=config.JOB_LOCATIONS, target_roles=config.JOB_ROLES)
        logger.info(f"Local auto-sync primed {len(fresh)} fresh jobs into database.")
        pipeline_status["message"] = f"Jobs primed. Daily digest scheduled for {config.REPORT_TIME}."
        pipeline_status["last_run"] = datetime.now().strftime("%H:%M")
    except Exception as e:
        logger.error(f"Error in _auto_sync_jobs_local: {e}", exc_info=True)
        pipeline_status["message"] = f"Auto-sync error: {e}"
    finally:
        pipeline_status["running"] = False


@app.post("/api/settings")
async def update_settings(request: Request):
    """
    Save user-specified configurations to Supabase (if authenticated) or local .env.
    Sends an immediate Telegram confirmation notification with updated criteria,
    and automatically initiates a background job search & evaluation matching the new settings.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    notification_sent = False
    auto_triggered = False

    if token and is_supabase_configured():
        try:
            u_info = decode_user_token(token)
            if u_info and u_info.get("id"):
                uid = u_info["id"]
                success = save_all_user_settings(uid, body, access_token=token)
                if success:
                    user_settings = get_all_user_settings(uid, access_token=token)
                    user_obj = get_user_by_id(uid)
                    if user_obj:
                        bot_token = user_obj.get("bot_token")
                        chat_id = user_obj.get("chat_id")
                        if bot_token and chat_id:
                            try:
                                notification_sent = send_settings_update_notification(
                                    settings={
                                        "job_locations": user_obj.get("locations", []),
                                        "job_roles": user_obj.get("roles", []),
                                        "experience_level": user_obj.get("experience_level", "fresher"),
                                        "report_time": user_obj.get("report_time", "09:41"),
                                        "scrape_interval_hours": user_obj.get("scrape_interval_hours", 23),
                                    },
                                    bot_token=bot_token,
                                    chat_id=chat_id,
                                )
                            except Exception as tg_err:
                                logger.error(f"Failed to send settings update Telegram notification: {tg_err}")

                        threading.Thread(
                            target=_auto_sync_jobs_for_user,
                            args=(user_obj,),
                            daemon=True
                        ).start()
                        auto_triggered = True

                    return {
                        "success": True,
                        "message": "Settings saved successfully! Telegram notification sent & automated job matching initiated.",
                        "storage_backend": "supabase",
                        "settings": user_settings,
                        "notification_sent": notification_sent,
                        "auto_job_search_triggered": auto_triggered,
                    }
        except Exception as e:
            logger.error(f"Error saving user settings to Supabase: {e}")

    # Local fallback
    updated = save_settings_to_env(body)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            notification_sent = send_settings_update_notification(
                settings={
                    "job_locations": config.JOB_LOCATIONS,
                    "job_roles": config.JOB_ROLES,
                    "experience_level": body.get("experience_level", "fresher"),
                    "report_time": config.REPORT_TIME,
                    "scrape_interval_hours": config.SCRAPE_INTERVAL_HOURS,
                },
                bot_token=config.TELEGRAM_BOT_TOKEN,
                chat_id=config.TELEGRAM_CHAT_ID,
            )
        except Exception as tg_err:
            logger.error(f"Failed to send local settings update Telegram notification: {tg_err}")

    threading.Thread(
        target=_auto_sync_jobs_local,
        args=(body.get("experience_level", "fresher"),),
        daemon=True
    ).start()
    auto_triggered = True

    return {
        "success": True,
        "message": "Configuration saved successfully and updated in .env! Telegram notification sent & pipeline initiated.",
        "storage_backend": "local",
        "settings": {
            "job_locations": config.JOB_LOCATIONS,
            "job_roles": config.JOB_ROLES,
            "telegram_bot_token": config.TELEGRAM_BOT_TOKEN,
            "telegram_chat_id": config.TELEGRAM_CHAT_ID,
            "report_time": config.REPORT_TIME,
            "scrape_interval_hours": config.SCRAPE_INTERVAL_HOURS,
        },
        "notification_sent": notification_sent,
        "auto_job_search_triggered": auto_triggered,
    }


@app.post("/api/test-telegram")
async def test_telegram_endpoint(request: Request):
    """
    Send a test notification message to verify Telegram credentials.
    Supports either passing temporary credentials in JSON or using saved credentials.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    bot_token = (body.get("telegram_bot_token") or "").strip()
    chat_id = (body.get("telegram_chat_id") or "").strip()

    # If not in body, extract from user's Supabase database settings
    if (not bot_token or not chat_id) and token and is_supabase_configured():
        try:
            u_info = decode_user_token(token)
            if u_info and u_info.get("id"):
                uid = u_info["id"]
                u_settings = get_all_user_settings(uid, access_token=token)
                bot_token = bot_token or u_settings.get("telegram_bot_token")
                chat_id = chat_id or u_settings.get("telegram_chat_id")
        except Exception as e:
            logger.warning(f"Could not load telegram settings from Supabase for test: {e}")

    if not bot_token or not chat_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Bot Token and Chat ID must both be provided."}
        )

    test_msg = (
        "<b>JobScout Notification Test</b>\n\n"
        "Your notifications are configured properly and ready to deliver daily job updates."
    )
    success = send_telegram_message(test_msg, parse_mode="HTML", bot_token=bot_token, chat_id=chat_id)
    if success:
        return {"success": True, "message": "Test notification sent successfully to Telegram!"}
    else:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Failed to send message. Verify your bot token and chat ID."}
        )


@app.post("/api/trigger-pipeline")
async def trigger_pipeline_endpoint(request: Request):
    """
    Trigger a full scrape, deduplicate, Gemini rank, and Telegram notification cycle
    in the background immediately.
    Extracts the authenticated user's locations, roles, and Telegram settings from Supabase.
    """
    from datetime import datetime
    from main import run_scraping_cycle, run_daily_ranking_and_dispatch, dispatch_for_user

    if not pipeline_lock.acquire(blocking=False):
        return {"success": False, "message": "A pipeline run is already in progress."}

    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    target_user = None
    if token and is_supabase_configured():
        try:
            u_info = decode_user_token(token)
            if u_info and u_info.get("id"):
                from database.user_service import get_user_by_id
                target_user = get_user_by_id(u_info["id"])
        except Exception as e:
            logger.warning(f"Could not resolve authenticated user for pipeline trigger: {e}")

    def _worker():
        pipeline_status["running"] = True
        pipeline_status["message"] = "Scraping & evaluating jobs..."
        try:
            if target_user:
                logger.info(
                    f"Manual trigger for user {target_user['user_id']}: "
                    f"locations={target_user.get('locations')}, roles={target_user.get('roles')}"
                )
                user_locs = target_user.get("locations") or []
                user_roles = target_user.get("roles") or []
                fresh = run_scraping_cycle(target_cities=user_locs, target_roles=user_roles, user_id=target_user["user_id"])
                dispatch_for_user(target_user, fresh)
            else:
                logger.info("Manual trigger: starting global pipeline execution...")
                run_scraping_cycle()
                run_daily_ranking_and_dispatch()

            pipeline_status["message"] = "Completed successfully."
            pipeline_status["last_run"] = datetime.now().strftime("%H:%M")
        except Exception as e:
            logger.error(f"Manual pipeline execution error: {e}")
            pipeline_status["message"] = f"Failed: {e}"
        finally:
            pipeline_status["running"] = False
            pipeline_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "success": True,
        "message": "Pipeline cycle initiated in background. You will receive the Telegram digest once completed!"
    }


@app.get("/api/recent-jobs")
async def recent_jobs_endpoint(limit: int = 25):
    """Retrieve recently stored jobs from SQLite database."""
    jobs = get_recent_jobs(limit=limit)
    return {"jobs": jobs, "total_count": get_total_jobs_count()}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    """Format dict as an SSE data frame."""
    return f"data: {json.dumps(data)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("  AI-JobScout Web Interface & Dashboard")
    logger.info("  URL: http://127.0.0.1:8000")
    logger.info("  API Docs: http://127.0.0.1:8000/docs")
    logger.info("═══════════════════════════════════════════════════════════")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")