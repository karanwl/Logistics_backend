import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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


def _error_payload(detail, message: str, code: str | None = None) -> dict:
    payload = {"ok": False, "error": {"message": message}}
    if code:
        payload["error"]["code"] = code
    if detail is not None:
        payload["error"]["details"] = detail
        payload["detail"] = detail
    return payload


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request failed."
    return JSONResponse(
        _error_payload(detail, message, f"HTTP_{exc.status_code}"),
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        _error_payload(exc.errors(), "Validation error.", "VALIDATION_ERROR"),
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(
        _error_payload("Internal server error.", "Internal server error.", "INTERNAL_ERROR"),
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
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
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
