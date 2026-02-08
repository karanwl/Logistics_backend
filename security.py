import secrets
from typing import Iterable

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from config import settings


_UNSAFE_METHODS: Iterable[str] = ("POST", "PUT", "PATCH", "DELETE")


def _get_cookie_token(request: Request) -> str | None:
    return request.cookies.get(settings.CSRF_COOKIE_NAME)


def get_csrf_token(request: Request) -> str:
    token = getattr(request.state, "csrf_token", None)
    if token:
        return token
    return _get_cookie_token(request) or ""


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=settings.CSRF_COOKIE_SECURE,
        samesite=settings.CSRF_COOKIE_SAMESITE,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
        max_age=60 * 60 * 24 * 30,
    )


async def csrf_protect(request: Request) -> None:
    if not settings.CSRF_ENABLED:
        return
    if request.method.upper() not in _UNSAFE_METHODS:
        return

    cookie_token = _get_cookie_token(request)
    if not cookie_token:
        raise HTTPException(status_code=403, detail="Missing CSRF cookie.")

    header_token = request.headers.get(settings.CSRF_HEADER_NAME)
    if header_token and secrets.compare_digest(header_token, cookie_token):
        return

    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        form_token = form.get("csrf_token")
        if form_token and secrets.compare_digest(str(form_token), cookie_token):
            return

    raise HTTPException(status_code=403, detail="CSRF validation failed.")
