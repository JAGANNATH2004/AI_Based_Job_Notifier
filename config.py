import os
from pathlib import Path
from dotenv import load_dotenv
import logging

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

# ── Global AI Engine ─────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── Supabase Cloud Configuration (User Database & Authentication) ────────────
SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY         = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# ── Cloud Database (100% Supabase PostgreSQL - Zero local SQLite files) ───────

# ── User Settings Defaults (Extracted per user from Supabase database) ────────
JOB_LOCATIONS: list = []
JOB_ROLES: list = []
TELEGRAM_BOT_TOKEN: str = ""
TELEGRAM_CHAT_ID: str = ""
REPORT_TIME: str = "09:41"
REPORT_TIMEZONE: str = os.getenv("REPORT_TIMEZONE", "Asia/Kolkata")
SCRAPE_INTERVAL_HOURS: int = 24

# Setup generic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def save_settings_to_env(settings: dict) -> dict:
    """
    Persist system infrastructure keys to .env.
    User-specific preferences (locations, roles, telegram credentials, scheduler)
    are saved to the user's Supabase database tables via database/user_service.py.
    """
    global GEMINI_API_KEY, GEMINI_MODEL
    global SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY

    env_lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            env_lines = f.readlines()

    current_env = {}
    for line in env_lines:
        line_s = line.strip()
        if line_s and not line_s.startswith("#") and "=" in line_s:
            k, v = line_s.split("=", 1)
            current_env[k.strip()] = v.strip().strip('"').strip("'")

    # Only save system-level keys to .env
    allowed_system_keys = [
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
    ]

    for key, val in settings.items():
        if val is None:
            continue
        key_upper = key.upper()
        if key_upper in allowed_system_keys:
            # Protect existing API key from being cleared accidentally if empty
            if key_upper == "GEMINI_API_KEY" and not str(val).strip():
                continue
            current_env[key_upper] = str(val).strip()

    # Write back to .env
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        for k, v in current_env.items():
            f.write(f"{k}={v}\n")

    # Reload into environment
    load_dotenv(dotenv_path=ENV_PATH, override=True)

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY         = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    logger.info("System configuration updated in .env.")
    return {
        "gemini_api_key": GEMINI_API_KEY,
        "gemini_model": GEMINI_MODEL,
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key_set": bool(SUPABASE_ANON_KEY),
    }