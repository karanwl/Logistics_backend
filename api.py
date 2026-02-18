import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app_errors import AppError, status_to_error_code
from config import settings
from models import ErrorResponse, ValidationErrorResponse
from responses import ok_response
from routers.auth_api import router as auth_api_router
from routers.driver_api import router as driver_router
from routers.planning_api import router as planning_router
from security import new_csrf_token, set_csrf_cookie

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

_DEFAULT_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request."},
    401: {"model": ErrorResponse, "description": "Unauthorized."},
    403: {"model": ErrorResponse, "description": "Forbidden."},
    404: {"model": ErrorResponse, "description": "Not found."},
    409: {"model": ErrorResponse, "description": "Conflict."},
    422: {"model": ValidationErrorResponse, "description": "Validation error."},
    500: {"model": ErrorResponse, "description": "Internal server error."},
}

app = FastAPI(title=settings.APP_NAME, version="0.5.0", responses=_DEFAULT_RESPONSES)


def _request_id(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(getattr(request, "state", None), "request_id", None)


def _error_payload(
    detail: Any,
    message: str,
    code: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    payload = {"ok": False, "error": {"message": message}}
    if code:
        payload["error"]["code"] = code
    if correlation_id:
        payload["error"]["correlationId"] = correlation_id
        payload["correlationId"] = correlation_id
    if detail is not None:
        payload["error"]["details"] = detail
        payload["detail"] = detail
    return payload


def _log_error_event(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    detail: Any | None = None,
    exc: Exception | None = None,
) -> None:
    level = logging.ERROR if status_code >= 500 else logging.WARNING
    logger.log(
        level,
        "event=api_error request_id=%s method=%s path=%s status=%s code=%s message=%s detail=%s",
        _request_id(request),
        request.method,
        request.url.path,
        status_code,
        code,
        message,
        detail if detail is None else str(detail),
        exc_info=exc,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    _log_error_event(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        detail=exc.details,
    )
    return JSONResponse(
        _error_payload(
            exc.details,
            exc.message,
            exc.code,
            _request_id(request),
        ),
        status_code=exc.status_code,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request failed."
    code = status_to_error_code(exc.status_code)
    _log_error_event(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        detail=detail,
    )
    return JSONResponse(
        _error_payload(detail, message, code, _request_id(request)),
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    _log_error_event(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        message="Validation error.",
        detail=exc.errors(),
    )
    return JSONResponse(
        _error_payload(
            exc.errors(),
            "Validation error.",
            "VALIDATION_ERROR",
            _request_id(request),
        ),
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    _log_error_event(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="Internal server error.",
        detail=str(exc),
        exc=exc,
    )
    return JSONResponse(
        _error_payload(
            "Internal server error.",
            "Internal server error.",
            "INTERNAL_ERROR",
            _request_id(request),
        ),
        status_code=500,
    )


@app.middleware("http")
async def csrf_cookie_middleware(request, call_next):
    if settings.CSRF_ENABLED:
        token = request.cookies.get(settings.CSRF_COOKIE_NAME)
        if not token:
            token = new_csrf_token()
            request.state.csrf_token = token
            response = await call_next(request)
            set_csrf_cookie(response, token)
            return response
        request.state.csrf_token = token
    else:
        request.state.csrf_token = ""
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    request_id = (
        request.headers.get("X-Request-ID")
        or request.headers.get("x-request-id")
        or request.headers.get("X-Correlation-ID")
        or request.headers.get("x-correlation-id")
        or ""
    ).strip() or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["x-correlation-id"] = request_id
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    logger.info(
        "request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response

# Uploads (driver photo proof)
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

# Routers
app.include_router(planning_router)
app.include_router(auth_api_router)
app.include_router(driver_router)


@app.get("/health")
def health(request: Request):
    return ok_response(request, {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV})
