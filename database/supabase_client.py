import os
import logging
from typing import Optional
from supabase import create_client, Client
import config

logger = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None
_supabase_admin: Optional[Client] = None


def _resolve_supabase_url() -> str:
    """Resolve valid https Supabase endpoint, auto-recovering project ref from JWT if needed."""
    url = os.getenv("SUPABASE_URL", "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    
    # If key contains JWT, extract the Supabase project ref
    for key_env in ["SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]:
        token = os.getenv(key_env, "").strip()
        if token and token.count(".") == 2:
            try:
                import jwt
                payload = jwt.decode(token, options={"verify_signature": False})
                ref = payload.get("ref")
                if ref:
                    return f"https://{ref}.supabase.co"
            except Exception:
                pass
    return url


def get_supabase_client(access_token: Optional[str] = None) -> Optional[Client]:
    """
    Returns a Supabase client configured with the public anon key.
    If an access_token (JWT) is provided, sets the Authorization header
    so requests obey Row Level Security (RLS) policies for that user.
    """
    url = _resolve_supabase_url()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()

    if not url or not key:
        return None

    try:
        client = create_client(url, key)
        if access_token:
            client.postgrest.auth(access_token)
        return client
    except Exception as e:
        logger.error(f"Error initializing Supabase client: {e}")
        return None


def get_supabase_admin_client() -> Optional[Client]:
    """
    Returns a Supabase client with the service role key (admin).
    This client bypasses RLS and is used by the background scheduler daemon
    to aggregate all users' schedules and dispatch alerts.
    Requires SUPABASE_SERVICE_ROLE_KEY to be explicitly set — will NOT
    fall back to the anon key (which would fail silently on RLS-protected tables).
    """
    global _supabase_admin
    if _supabase_admin is not None:
        return _supabase_admin

    url = _resolve_supabase_url()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not url or not key:
        if not key:
            logger.warning(
                "SUPABASE_SERVICE_ROLE_KEY is not set. Background scheduler and "
                "admin operations will be limited. Add it to your .env file."
            )
        return None

    try:
        _supabase_admin = create_client(url, key)
        return _supabase_admin
    except Exception as e:
        logger.error(f"Error initializing Supabase admin client: {e}")
        return None


def is_supabase_configured() -> bool:
    """Returns True if Supabase URL and Anon Key are present."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    return bool(url and key)
