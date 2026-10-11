"""
notifier/telegram_notifier.py
Sends formatted job listings to a Telegram chat or channel using Telegram Bot API.
"""
import logging
import os
import sys
import html
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def send_telegram_message(
    text: str,
    parse_mode: str = "HTML",
    bot_token: str = None,
    chat_id: str = None
) -> bool:
    """
    Send a raw message via the Telegram Bot API.
    Supports either user-specified credentials or global default credentials.
    Handles message splitting if length exceeds Telegram's 4096 char limit.
    """
    token = (bot_token or TELEGRAM_BOT_TOKEN or "").strip()
    chat = (chat_id or TELEGRAM_CHAT_ID or "").strip()

    if not token or not chat:
        logger.warning("Telegram Bot Token or Chat ID is missing in configuration.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram limit is 4096 characters per message.
    # Split on double-newline boundaries to avoid breaking mid-HTML-tag.
    MAX_CHUNK = 4000
    if len(text) <= MAX_CHUNK:
        chunks = [text]
    else:
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_CHUNK:
                chunks.append(remaining)
                break
            # Find the last safe split point (double newline) within the limit
            split_pos = remaining.rfind("\n\n", 0, MAX_CHUNK)
            if split_pos <= 0:
                # Fallback: split on single newline
                split_pos = remaining.rfind("\n", 0, MAX_CHUNK)
            if split_pos <= 0:
                # Last resort: hard split at limit
                split_pos = MAX_CHUNK
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:].lstrip("\n")

    success = True
    for chunk in chunks:
        payload = {
            "chat_id": chat,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            logger.info("Telegram message sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            success = False

    return success


def format_jobs_digest(jobs: list, profile: dict = None) -> str:
    """
    Format a list of ranked jobs into a clean, readable HTML message for Telegram.
    Dynamically customizes title and subtitle according to user's configured roles and experience.
    """
    today_str = datetime.now().strftime("%B %d, %Y")
    if not jobs:
        return f"🤖 <b>AI-JobScout Digest — {today_str}</b>\n\nNo fresh roles matched today's criteria."

    roles = profile.get("roles") if profile else None
    exp = profile.get("experience_level") if profile else None

    if roles:
        roles_title = ", ".join(roles[:2])
        if len(roles) > 2:
            roles_title += " & More"
    else:
        roles_title = "Tech & Engineering"

    exp_map = {
        "fresher": "0-1 yr exp / Fresher",
        "0-2 years": "0-2 yrs exp / Junior",
        "1-3 years": "1-3 yrs exp / Associate",
        "3-5 years": "3-5 yrs exp / Mid-Senior",
        "5+ years": "5+ yrs exp / Senior",
        "any": "All Experience Levels",
    }
    exp_text = exp_map.get(str(exp).lower(), "Curated Matches") if exp else "Curated Matches"

    header = f"🚀 <b>{html.escape(roles_title)} Job Digest — {today_str}</b>\n"
    header += f"🎯 <i>Top {len(jobs)} Opportunities ({html.escape(exp_text)})</i>\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

    items = []
    for idx, job in enumerate(jobs, 1):
        title = html.escape(job.get("title", "Job Title"))
        company = html.escape(job.get("company", "Unknown Company"))
        location = html.escape(job.get("location", "India"))
        job_exp = html.escape(job.get("experience", "Not specified"))
        source = html.escape(job.get("source", "Web"))
        posted = html.escape(job.get("posted_time", "Recently"))
        link = html.escape(job.get("link", "").strip(), quote=True)

        # Validate link scheme
        if link and not link.startswith(("http://", "https://")):
            link = ""

        item = (
            f"<b>{idx}. {title}</b>\n"
            f"🏢 <b>Company:</b> {company}\n"
            f"📍 <b>Location:</b> {location}\n"
            f"💼 <b>Exp:</b> {job_exp} | 🕒 <b>Posted:</b> {posted} | 🏷 <b>Source:</b> {source}\n"
        )
        if link:
            item += f'🔗 <a href="{link}"><b>Apply Here ➔</b></a>\n'
        else:
            item += "🔗 <i>Link unavailable</i>\n"
        items.append(item)

    footer = "\n━━━━━━━━━━━━━━━━━━━━━━\n🤖 <i>Curated with Google Gemini AI & Automated Pipeline</i>"
    return header + "\n".join(items) + footer


def notify_top_jobs(jobs: list, bot_token: str = None, chat_id: str = None, profile: dict = None) -> bool:
    """Convenience function to format and send the top jobs to Telegram."""
    if not jobs:
        logger.info("No jobs to notify.")
        return False
    message = format_jobs_digest(jobs, profile=profile)
    return send_telegram_message(message, parse_mode="HTML", bot_token=bot_token, chat_id=chat_id)


def send_settings_update_notification(settings: dict, bot_token: str = None, chat_id: str = None) -> bool:
    """
    Sends a confirmation message to Telegram when the user updates their preferences.
    """
    locs = settings.get("job_locations") or []
    roles = settings.get("job_roles") or []
    exp = settings.get("experience_level") or "fresher"
    report_time = settings.get("report_time") or "09:41"
    interval = settings.get("scrape_interval_hours") or 23

    locs_str = ", ".join(locs) if locs else "All Locations / Remote"
    roles_str = ", ".join(roles) if roles else "All Tech Roles"

    exp_map = {
        "fresher": "Fresher / Entry-Level (0-1 yrs)",
        "0-2 years": "Junior (0-2 years)",
        "1-3 years": "Associate (1-3 years)",
        "3-5 years": "Mid-Senior (3-5 years)",
        "5+ years": "Senior / Lead (5+ years)",
        "any": "All Experience Levels",
    }
    exp_display = exp_map.get(str(exp).lower(), exp)

    message = (
        "⚙️ <b>Settings Updated Successfully!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Locations:</b> {html.escape(locs_str)}\n"
        f"💼 <b>Target Roles:</b> {html.escape(roles_str)}\n"
        f"🎓 <b>Experience:</b> {html.escape(exp_display)}\n"
        f"⏰ <b>Daily Digest Schedule:</b> {html.escape(str(report_time))} (Asia/Kolkata)\n"
        f"🔄 <b>Scrape Frequency:</b> Every {interval} hours\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <i>Your daily job digest is scheduled for <b>{html.escape(str(report_time))}</b>. Curated opportunities will be delivered to your Telegram at your scheduled time!</i>"
    )
    return send_telegram_message(message, parse_mode="HTML", bot_token=bot_token, chat_id=chat_id)
