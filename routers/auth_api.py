import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from auth import clear_session, get_current_user, issue_session, maybe_upgrade_password, verify_password
from config import settings
from security import csrf_protect
from responses import ok_response
from services import DatabaseService

router = APIRouter(dependencies=[Depends(csrf_protect)])
logger = logging.getLogger(__name__)


def _redact_user_record(rec: dict | None) -> dict | None:
    if not rec:
        return rec
    fields = dict(rec.get("fields", {}) or {})
    if settings.USERS_PASSWORD_FIELD in fields:
        fields[settings.USERS_PASSWORD_FIELD] = "<redacted>"
    return {"id": rec.get("id"), "fields": fields}


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    if settings.AUTH_DEBUG:
        logger.info("Login form input email=%s password_len=%s", email, len(password or ""))
    svc = DatabaseService()
    rec = svc.get_user_by_email(email.strip())
    if settings.AUTH_DEBUG:
        logger.info("Login DB user record=%s", _redact_user_record(rec))
    if not rec:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    fields = rec.get("fields", {})
    if not fields.get(settings.USERS_ACTIVE_FIELD, True):
        raise HTTPException(status_code=403, detail="Account disabled. Contact manager/admin.")

    stored = fields.get(settings.USERS_PASSWORD_FIELD, "") or ""
    if not verify_password(password, stored):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    maybe_upgrade_password(rec.get("id"), password, stored)

    try:
        session = issue_session(rec.get("id"))
    except Exception:
        logger.exception("Login failed while issuing session for user_id=%s", rec.get("id"))
        raise HTTPException(
            status_code=503,
            detail="Login is temporarily unavailable. Verify users table migrations.",
        )
    resp = ok_response(request, {"ok": True})
    resp.set_cookie(
        settings.AUTH_COOKIE_NAME,
        session["token"],
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        secure=settings.AUTH_COOKIE_SECURE,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path=settings.AUTH_COOKIE_PATH,
        max_age=settings.SESSION_DAYS * 24 * 60 * 60,
    )
    return resp


@router.get("/logout")
def logout(request: Request):
    user = get_current_user(request)
    if user:
        clear_session(user["record_id"])
    resp = ok_response(request, {"ok": True})
    resp.delete_cookie(
        settings.AUTH_COOKIE_NAME,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path=settings.AUTH_COOKIE_PATH,
    )
    return resp
