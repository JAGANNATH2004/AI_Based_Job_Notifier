import os
import uuid
import datetime
import logging
from typing import Optional
import bcrypt
import jwt
from fastapi import HTTPException, Security, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from database.supabase_client import get_supabase_client, get_supabase_admin_client, is_supabase_configured

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 30


def _get_or_create_jwt_secret() -> str:
    """
    Retrieve JWT_SECRET from environment. If not set, generate a
    cryptographically secure random key and persist it to the .env file
    so it survives restarts. Never falls back to SUPABASE_ANON_KEY or hardcoded strings.
    """
    secret = os.getenv("JWT_SECRET", "").strip()
    if secret:
        return secret

    import secrets
    from pathlib import Path

    secret = secrets.token_urlsafe(48)  # 64-char URL-safe random string
    logger.warning("JWT_SECRET not found in .env — generating a secure random key and persisting it.")

    env_path = Path(__file__).parent.parent / ".env"
    try:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nJWT_SECRET={secret}\n")
        os.environ["JWT_SECRET"] = secret
    except Exception as e:
        logger.error(f"Could not persist JWT_SECRET to .env: {e}")

    return secret


JWT_SECRET = _get_or_create_jwt_secret()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: Optional[str] = ""


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt with random salt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Verify plaintext password against stored bcrypt or Blowfish hash."""
    if not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception as e:
        logger.warning(f"Error checking password hash: {e}")
        return False


def create_jwt_token(user_id: str, email: str, full_name: str = "") -> str:
    """Generate signed JWT access token valid for 30 days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "full_name": full_name or "",
        "iat": now,
        "exp": now + datetime.timedelta(days=JWT_EXPIRATION_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_user_token(token: str) -> Optional[dict]:
    """
    Decode user session from token.
    Supports our custom HMAC-SHA256 JWT as well as legacy Supabase tokens.
    """
    if not token:
        return None

    # 1. Try decoding as application JWT
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {
            "id": str(payload.get("sub")),
            "email": payload.get("email", ""),
            "full_name": payload.get("full_name", ""),
        }
    except Exception:
        pass

    # 2. Fallback to Supabase GoTrue token validation
    try:
        client = get_supabase_client()
        if client:
            res = client.auth.get_user(token)
            if res and res.user:
                return {
                    "id": str(res.user.id),
                    "email": res.user.email,
                    "full_name": (res.user.user_metadata or {}).get("full_name", ""),
                }
    except Exception:
        pass

    return None


def ensure_user_profile(user_id: str, email: str, full_name: str = "", password_hash: Optional[str] = None):
    """Guarantee profile row exists in public.profiles to satisfy foreign keys."""
    admin = get_supabase_admin_client() or get_supabase_client()
    if not admin:
        return
    try:
        payload = {
            "id": user_id,
            "email": email,
            "full_name": full_name or ""
        }
        if password_hash:
            payload["password_hash"] = password_hash
        admin.table("profiles").upsert(payload, on_conflict="id").execute()
    except Exception as e:
        logger.warning(f"ensure_user_profile error for {user_id}: {e}")


def register_user(data: RegisterRequest) -> dict:
    """
    Register a new user directly in the Supabase PostgreSQL database.
    Hashes the password with bcrypt, stores in users/profiles table,
    and returns a signed JWT token without triggering email limits.
    """
    if not is_supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Please add SUPABASE_URL and SUPABASE_ANON_KEY to your .env file."
        )

    admin = get_supabase_admin_client() or get_supabase_client()
    if not admin:
        raise HTTPException(status_code=503, detail="Database client unavailable.")

    clean_email = str(data.email).lower().strip()
    full_name = (data.full_name or "").strip()

    # Check if user already exists
    try:
        existing = admin.table("users").select("id").eq("email", clean_email).execute()
        if existing and existing.data:
            raise HTTPException(status_code=400, detail="An account with this email already exists. Please log in.")
    except HTTPException:
        raise
    except Exception as e:
        logger.info(f"Checking existing users table (may not exist yet): {e}")

    # Also check profiles table
    try:
        existing_prof = admin.table("profiles").select("id").eq("email", clean_email).execute()
        if existing_prof and existing_prof.data:
            raise HTTPException(status_code=400, detail="An account with this email already exists. Please log in.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Checking profiles table: {e}")

    # Generate secure bcrypt hash
    pwd_hash = hash_password(data.password)
    user_id = str(uuid.uuid4())

    user_record = {
        "id": user_id,
        "email": clean_email,
        "password_hash": pwd_hash,
        "full_name": full_name,
    }

    # 1. Insert into users table
    inserted = False
    try:
        admin.table("users").insert(user_record).execute()
        inserted = True
    except Exception as e:
        logger.warning(f"Could not insert into 'users' table directly: {e}")

    # 2. Insert into profiles table (ensuring child table foreign keys work)
    try:
        admin.table("profiles").upsert(user_record, on_conflict="id").execute()
    except Exception as e:
        logger.warning(f"Could not upsert into 'profiles' table: {e}")
        if not inserted:
            raise HTTPException(status_code=500, detail=f"Failed to create account in database: {e}")

    # 3. Auto-provision default settings
    try:
        admin.table("user_job_preferences").upsert({
            "user_id": user_id,
            "locations": ["Hyderabad"],
            "roles": ["Backend Engineer"],
            "experience_level": "fresher"
        }, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning(f"Auto-provision job preferences: {e}")

    try:
        admin.table("user_telegram_settings").upsert({
            "user_id": user_id
        }, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning(f"Auto-provision telegram settings: {e}")

    try:
        admin.table("user_scheduler_settings").upsert({
            "user_id": user_id,
            "report_time": "09:41",
            "scrape_interval_hours": 24,
            "is_enabled": True
        }, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning(f"Auto-provision scheduler settings: {e}")

    # 4. Generate JWT session token
    token = create_jwt_token(user_id, clean_email, full_name)

    return {
        "success": True,
        "message": "Account created successfully!",
        "user": {
            "id": user_id,
            "email": clean_email,
            "full_name": full_name,
        },
        "session": {
            "access_token": token,
            "refresh_token": token,
            "token_type": "bearer",
        },
    }


def login_user(data: LoginRequest) -> dict:
    """
    Log in a user by checking the hashed password from the database table.
    """
    if not is_supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Please add SUPABASE_URL and SUPABASE_ANON_KEY to your .env file."
        )

    admin = get_supabase_admin_client() or get_supabase_client()
    if not admin:
        raise HTTPException(status_code=503, detail="Database client unavailable.")

    clean_email = str(data.email).lower().strip()

    # 1. Search in users table first
    user_row = None
    try:
        res = admin.table("users").select("*").eq("email", clean_email).execute()
        if res and res.data:
            user_row = res.data[0]
    except Exception as e:
        logger.info(f"Error querying users table: {e}")

    # 2. If not found in users table, search in profiles table
    if not user_row:
        try:
            res = admin.table("profiles").select("*").eq("email", clean_email).execute()
            if res and res.data:
                user_row = res.data[0]
        except Exception as e:
            logger.info(f"Error querying profiles table: {e}")

    # 3. If found with password_hash, verify credentials
    if user_row and user_row.get("password_hash"):
        stored_hash = user_row["password_hash"]
        if verify_password(data.password, stored_hash):
            uid = str(user_row["id"])
            full_name = user_row.get("full_name", "")
            token = create_jwt_token(uid, clean_email, full_name)
            return {
                "success": True,
                "message": "Login successful!",
                "user": {
                    "id": uid,
                    "email": clean_email,
                    "full_name": full_name,
                },
                "session": {
                    "access_token": token,
                    "refresh_token": token,
                    "token_type": "bearer",
                }
            }
        else:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

    # 4. Fallback: Check if user registered via legacy Supabase GoTrue Auth
    try:
        client = get_supabase_client()
        res = client.auth.sign_in_with_password({
            "email": clean_email,
            "password": data.password
        })
        if res.user and res.session:
            uid = str(res.user.id)
            name_str = (res.user.user_metadata or {}).get("full_name", "")
            # Sync password hash into users/profiles for fast future logins
            try:
                pwd_hash = hash_password(data.password)
                admin.table("profiles").upsert({
                    "id": uid,
                    "email": clean_email,
                    "full_name": name_str,
                    "password_hash": pwd_hash
                }, on_conflict="id").execute()
            except Exception:
                pass

            token = create_jwt_token(uid, clean_email, name_str)
            return {
                "success": True,
                "message": "Login successful!",
                "user": {
                    "id": uid,
                    "email": clean_email,
                    "full_name": name_str,
                },
                "session": {
                    "access_token": token,
                    "refresh_token": token,
                    "token_type": "bearer",
                }
            }
    except Exception:
        pass

    raise HTTPException(status_code=401, detail="Invalid email or password.")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """
    FastAPI dependency to extract and authenticate the active user from the Bearer token.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    user_info = decode_user_token(token)

    if not user_info or not user_info.get("id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please log in again."
        )

    return {
        "id": user_info["id"],
        "email": user_info.get("email", ""),
        "full_name": user_info.get("full_name", ""),
        "token": token
    }
