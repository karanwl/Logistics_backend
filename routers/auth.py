import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from auth import clear_session, get_current_user, hash_password, issue_session, verify_password, maybe_upgrade_password
from config import settings
from security import csrf_protect, get_csrf_token
from services import DatabaseService

router = APIRouter(dependencies=[Depends(csrf_protect)])
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


def _redact_user_record(rec: dict | None) -> dict | None:
    if not rec:
        return rec
    fields = dict(rec.get("fields", {}) or {})
    if settings.USERS_PASSWORD_FIELD in fields:
        fields[settings.USERS_PASSWORD_FIELD] = "<redacted>"
    return {"id": rec.get("id"), "fields": fields}


def _redirect(path: str):
    return RedirectResponse(path, status_code=303)


def _template(request: Request, name: str, context: dict | None = None):
    payload = {"request": request, "csrf_token": get_csrf_token(request)}
    if context:
        payload.update(context)
    return templates.TemplateResponse(name, payload)


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request):
    user = get_current_user(request)
    if user:
        return _redirect("/")
    return _template(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    if settings.AUTH_DEBUG:
        logger.info("Login form input email=%s password_len=%s", email, len(password or ""))
    svc = DatabaseService()
    rec = svc.get_user_by_email(email.strip())
    if settings.AUTH_DEBUG:
        logger.info("Login DB user record=%s", _redact_user_record(rec))
    if not rec:
        return _template(request, "login.html", {"error": "Invalid email or password."})

    fields = rec.get("fields", {})
    if not fields.get(settings.USERS_ACTIVE_FIELD, True):
        return _template(request, "login.html", {"error": "Account disabled. Contact manager/admin."})

    stored = fields.get(settings.USERS_PASSWORD_FIELD, "") or ""
    if not verify_password(password, stored):
        return _template(request, "login.html", {"error": "Invalid email or password."})
    maybe_upgrade_password(rec.get("id"), password, stored)

    session = issue_session(rec.get("id"))
    resp = _redirect("/")
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


@router.get("/signup", response_class=HTMLResponse)
def signup_view(request: Request):
    user = get_current_user(request)
    if user:
        return _redirect("/")
    return _template(
        request,
        "signup.html",
        {"message": "Account creation is managed by your administrator. Please contact admin to request access."},
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
):
    return _template(
        request,
        "signup.html",
        {"message": "Account creation is managed by your administrator. Please contact admin to request access."},
    )


@router.get("/logout")
def logout(request: Request):
    user = get_current_user(request)
    if user:
        clear_session(user["record_id"])
    resp = _redirect("/login")
    resp.delete_cookie(
        settings.AUTH_COOKIE_NAME,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path=settings.AUTH_COOKIE_PATH,
    )
    return resp
