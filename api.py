import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import settings
from routers.auth_api import router as auth_api_router
from routers.driver_api import router as driver_router
from routers.planning_api import router as planning_router
from security import new_csrf_token, set_csrf_cookie

logging.basicConfig(level=settings.LOG_LEVEL)

app = FastAPI(title=settings.APP_NAME, version="0.5.0")


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
def health():
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}
