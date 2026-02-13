import base64
import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from config import settings
from services import DatabaseService


_PBKDF2_ITERATIONS = 200_000
_AUTH_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: dict[str, dict[str, Any]] = {}
logger = logging.getLogger(__name__)


def _cache_get(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _AUTH_CACHE_LOCK:
        entry = _AUTH_CACHE.get(token)
    if not entry:
        return None
    expires = entry.get("cache_expires")
    if isinstance(expires, datetime) and expires < _now_utc():
        _cache_evict(token)
        return None
    return entry.get("user")


def _cache_set(token: str, user: Dict[str, Any]) -> None:
    if not token:
        return
    ttl = max(0, int(settings.AUTH_SESSION_CACHE_SECONDS or 0))
    if ttl <= 0:
        return
    expires = _now_utc() + timedelta(seconds=ttl)
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE[token] = {"user": user, "cache_expires": expires}


def _cache_evict(token: str) -> None:
    if not token:
        return
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE.pop(token, None)


def invalidate_cached_token(token: str) -> None:
    _cache_evict(token)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    if password is None:
        raise ValueError("password required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(dk)}"


def verify_password(password: str, stored: str) -> bool:
    stored = (stored or "").strip()
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            algo, iter_str, salt_b64, hash_b64 = stored.split("$", 3)
            if algo != "pbkdf2_sha256":
                return False
            iterations = int(iter_str)
            salt = _b64decode(salt_b64)
            expected = _b64decode(hash_b64)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False
    # Fallback for legacy/plaintext passwords (migration safety).
    return hmac.compare_digest(password, stored)


def maybe_upgrade_password(user_record_id: str, password: str, stored: str) -> None:
    if not user_record_id:
        return
    stored = (stored or "").strip()
    if not stored or stored.startswith("pbkdf2_sha256$"):
        return
    if not hmac.compare_digest(password, stored):
        return
    try:
        DatabaseService().update_record(
            settings.USERS_TABLE,
            user_record_id,
            {settings.USERS_PASSWORD_FIELD: hash_password(password)},
        )
    except Exception:
        # Best-effort upgrade; login should still succeed.
        logger.debug("Password hash upgrade skipped for user %s", user_record_id, exc_info=True)
        return


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(val: str) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def issue_session(user_record_id: str) -> Dict[str, Any]:
    token = secrets.token_urlsafe(32)
    expires = _now_utc() + timedelta(days=settings.SESSION_DAYS)
    DatabaseService().update_user_session(user_record_id, token, _isoformat(expires))
    _cache_evict(token)
    return {"token": token, "expires": expires}


def clear_session(user_record_id: str):
    DatabaseService().update_user_session(user_record_id, None, None)


def get_current_user(request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        return None
    return get_user_from_token(token)


def get_user_from_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    cached = _cache_get(token)
    if cached:
        return cached
    svc = DatabaseService()
    rec = svc.get_user_by_session_token(token)
    if not rec:
        return None
    fields = rec.get("fields", {})
    if not fields.get(settings.USERS_ACTIVE_FIELD, True):
        return None
    exp_raw = fields.get(settings.USERS_SESSION_EXPIRES_FIELD)
    exp = _parse_dt(exp_raw) if exp_raw else None
    if exp and exp < _now_utc():
        try:
            clear_session(rec.get("id"))
        except Exception:
            logger.debug("Failed to clear expired session for user %s", rec.get("id"), exc_info=True)
        return None
    name = fields.get(settings.USERS_NAME_FIELD)
    email = fields.get(settings.USERS_EMAIL_FIELD)
    display = name or email
    user = {
        "record_id": rec.get("id"),
        "email": email,
        "name": name,
        "display_name": display,
        "role": fields.get(settings.USERS_ROLE_FIELD) or "coordinator",
    }
    _cache_set(token, user)
    return user
