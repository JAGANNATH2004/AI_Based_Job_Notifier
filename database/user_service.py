import logging
from typing import Optional, List, Dict, Any, Set
from database.supabase_client import get_supabase_client, get_supabase_admin_client, is_supabase_configured

logger = logging.getLogger(__name__)


def get_all_user_settings(user_id: str, access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch consolidated settings for a specific user across Supabase tables:
      - user_job_preferences
      - user_telegram_settings
      - user_scheduler_settings
    """
    if not is_supabase_configured():
        return {}

    client = get_supabase_admin_client() or get_supabase_client(access_token=access_token)
    if not client:
        return {}

    try:
        # 1. Preferences
        pref_res = client.table("user_job_preferences").select("*").eq("user_id", user_id).execute()
        pref = pref_res.data[0] if pref_res.data else {}

        # 2. Telegram settings
        tg_res = client.table("user_telegram_settings").select("*").eq("user_id", user_id).execute()
        tg = tg_res.data[0] if tg_res.data else {}

        # 3. Scheduler settings
        sch_res = client.table("user_scheduler_settings").select("*").eq("user_id", user_id).execute()
        sch = sch_res.data[0] if sch_res.data else {}

        return {
            "job_locations": pref.get("locations", []),
            "job_roles": pref.get("roles", []),
            "experience_level": pref.get("experience_level", "fresher"),
            "telegram_bot_token": tg.get("bot_token") or "",
            "telegram_chat_id": tg.get("chat_id") or "",
            "report_time": sch.get("report_time", "09:41"),
            "timezone": sch.get("timezone", "Asia/Kolkata"),
            "scrape_interval_hours": sch.get("scrape_interval_hours", 24),
            "is_scheduler_enabled": sch.get("is_enabled", True),
        }
    except Exception as e:
        logger.error(f"Error fetching user settings from Supabase for {user_id}: {e}")
        return {}


def save_all_user_settings(user_id: str, settings: Dict[str, Any], access_token: Optional[str] = None) -> bool:
    """
    Update or insert user settings across Supabase tables:
      - user_job_preferences (locations, roles, experience_level)
      - user_telegram_settings (bot_token, chat_id)
      - user_scheduler_settings (report_time, scrape_interval_hours)
    """
    if not is_supabase_configured():
        return False

    client = get_supabase_admin_client() or get_supabase_client(access_token=access_token)
    if not client:
        return False

    try:
        # 1. Update job preferences (if any preference provided)
        locs = settings.get("job_locations")
        roles = settings.get("job_roles")
        exp = settings.get("experience_level")

        if locs is not None or roles is not None or exp is not None:
            pref_payload = {"user_id": user_id}
            if locs is not None:
                pref_payload["locations"] = locs if isinstance(locs, list) else [s.strip() for s in locs.split(",") if s.strip()]
            if roles is not None:
                pref_payload["roles"] = roles if isinstance(roles, list) else [s.strip() for s in roles.split(",") if s.strip()]
            if exp is not None:
                pref_payload["experience_level"] = exp
            client.table("user_job_preferences").upsert(pref_payload, on_conflict="user_id").execute()

        # 2. Update Telegram settings (if any Telegram credential provided)
        tg_token = settings.get("telegram_bot_token")
        tg_chat = settings.get("telegram_chat_id")
        if tg_token is not None or tg_chat is not None:
            tg_payload = {"user_id": user_id}
            if tg_token is not None:
                tg_payload["bot_token"] = str(tg_token).strip()
            if tg_chat is not None:
                tg_payload["chat_id"] = str(tg_chat).strip()
            client.table("user_telegram_settings").upsert(tg_payload, on_conflict="user_id").execute()

        # 3. Update scheduler settings (if any scheduler setting provided)
        report_time = settings.get("report_time")
        interval = settings.get("scrape_interval_hours")
        if report_time is not None or interval is not None:
            sch_payload = {"user_id": user_id}
            if report_time is not None:
                sch_payload["report_time"] = str(report_time).strip()
            if interval is not None:
                try:
                    sch_payload["scrape_interval_hours"] = int(interval)
                except (ValueError, TypeError):
                    pass
            client.table("user_scheduler_settings").upsert(sch_payload, on_conflict="user_id").execute()
        return True
    except Exception as e:
        logger.error(f"Error saving user settings to Supabase for {user_id}: {e}")
        return False


def get_user_delivered_links(user_id: str) -> Set[str]:
    """Retrieve set of job links previously delivered to this user (for deduplication)."""
    admin = get_supabase_admin_client()
    if not admin:
        return set()

    try:
        res = admin.table("user_delivered_jobs").select("job_link").eq("user_id", user_id).execute()
        return {row["job_link"] for row in (res.data or []) if row.get("job_link")}
    except Exception as e:
        logger.error(f"Error fetching delivered links for user {user_id}: {e}")
        return set()


def record_delivered_jobs(user_id: str, jobs: List[Dict[str, Any]]) -> None:
    """Record delivered jobs in user_delivered_jobs table to prevent future duplicates."""
    admin = get_supabase_admin_client()
    if not admin or not jobs:
        return

    records = []
    for j in jobs:
        link = j.get("link")
        if link:
            records.append({
                "user_id": user_id,
                "job_link": link,
                "match_score": j.get("score") or j.get("match_score"),
            })

    try:
        admin.table("user_delivered_jobs").upsert(records, on_conflict="user_id,job_link").execute()
    except Exception as e:
        logger.error(f"Error recording delivered jobs for user {user_id}: {e}")


def get_all_active_scheduled_users() -> List[Dict[str, Any]]:
    """
    Fetch all active users configured for daily scheduled job digests.
    Used by the background scheduler daemon.
    """
    admin = get_supabase_admin_client()
    if not admin:
        return []

    try:
        # Join scheduler, telegram, and preferences tables
        res = admin.table("user_scheduler_settings").select(
            "user_id, report_time, timezone, scrape_interval_hours, is_enabled, last_dispatched_at"
        ).eq("is_enabled", True).execute()

        users = []
        for s in (res.data or []):
            uid = s["user_id"]
            # Fetch telegram settings
            tg_res = admin.table("user_telegram_settings").select("bot_token, chat_id").eq("user_id", uid).execute()
            tg = tg_res.data[0] if tg_res.data else {}

            if not tg.get("bot_token") or not tg.get("chat_id"):
                continue  # Skip users without configured telegram bot

            # Fetch preferences
            pref_res = admin.table("user_job_preferences").select("locations, roles, experience_level").eq("user_id", uid).execute()
            pref = pref_res.data[0] if pref_res.data else {}

            users.append({
                "user_id": uid,
                "report_time": s.get("report_time", "09:41"),
                "timezone": s.get("timezone", "Asia/Kolkata"),
                "scrape_interval_hours": s.get("scrape_interval_hours", 24),
                "last_dispatched_at": s.get("last_dispatched_at"),
                "bot_token": tg.get("bot_token"),
                "chat_id": tg.get("chat_id"),
                "locations": pref.get("locations", []),
                "roles": pref.get("roles", []),
                "experience_level": pref.get("experience_level", "fresher"),
            })
        return users
    except Exception as e:
        logger.error(f"Error fetching active scheduled users: {e}")
        return []


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch complete profile and preferences for a single user."""
    admin = get_supabase_admin_client()
    if not admin:
        return None

    try:
        pref_res = admin.table("user_job_preferences").select("locations, roles, experience_level").eq("user_id", user_id).execute()
        pref = pref_res.data[0] if pref_res.data else {}

        tg_res = admin.table("user_telegram_settings").select("bot_token, chat_id").eq("user_id", user_id).execute()
        tg = tg_res.data[0] if tg_res.data else {}

        sch_res = admin.table("user_scheduler_settings").select("report_time, timezone, scrape_interval_hours, is_enabled, last_dispatched_at").eq("user_id", user_id).execute()
        sch = sch_res.data[0] if sch_res.data else {}

        return {
            "user_id": user_id,
            "locations": pref.get("locations", []),
            "roles": pref.get("roles", []),
            "experience_level": pref.get("experience_level", "fresher"),
            "bot_token": tg.get("bot_token", ""),
            "chat_id": tg.get("chat_id", ""),
            "report_time": sch.get("report_time", "09:41"),
            "timezone": sch.get("timezone", "Asia/Kolkata"),
            "scrape_interval_hours": sch.get("scrape_interval_hours", 24),
            "is_enabled": sch.get("is_enabled", True),
            "last_dispatched_at": sch.get("last_dispatched_at"),
        }
    except Exception as e:
        logger.error(f"Error fetching user by ID {user_id}: {e}")
        return None


def update_user_last_dispatched(user_id: str) -> bool:
    """Update last_dispatched_at timestamp in user_scheduler_settings to current time."""
    admin = get_supabase_admin_client()
    if not admin:
        return False
    try:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        admin.table("user_scheduler_settings").update({"last_dispatched_at": now_iso}).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating last_dispatched_at for {user_id}: {e}")
        return False


def save_scraped_jobs_to_supabase(jobs: List[Dict[str, Any]]) -> int:
    """Upsert fresh scraped listings into Supabase scraped_jobs table."""
    admin = get_supabase_admin_client()
    if not admin or not jobs:
        return 0

    records = []
    for j in jobs:
        link = j.get("link", "").strip()
        if not link:
            continue
        fingerprint = link.lower()
        records.append({
            "title": j.get("title", "Unknown Role"),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "source": j.get("source", "Web"),
            "link": link,
            "experience": j.get("experience", ""),
            "description": j.get("description", ""),
            "posted_date": j.get("posted_date", ""),
            "fingerprint": fingerprint,
        })

    if not records:
        return 0

    try:
        admin.table("scraped_jobs").upsert(records, on_conflict="fingerprint").execute()
        return len(records)
    except Exception as e:
        logger.warning(f"Could not sync scraped jobs to Supabase: {e}")
        return 0


def get_recent_scraped_jobs_from_supabase(limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve recent scraped listings from Supabase scraped_jobs table."""
    admin = get_supabase_admin_client()
    if not admin:
        return []
    try:
        res = admin.table("scraped_jobs").select("*").order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        logger.warning(f"Could not load scraped jobs from Supabase: {e}")
        return []


def get_scraped_jobs_count_from_supabase() -> int:
    """Retrieve total count of scraped listings in Supabase scraped_jobs table."""
    admin = get_supabase_admin_client()
    if not admin:
        return 0
    try:
        res = admin.table("scraped_jobs").select("id", count="exact").execute()
        return res.count if res.count is not None else len(res.data or [])
    except Exception as e:
        logger.warning(f"Could not count scraped jobs from Supabase: {e}")
        return 0
