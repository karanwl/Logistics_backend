from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from starlette.requests import Request


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def wants_envelope(request: Request | None) -> bool:
    if request is None:
        return False
    header = request.headers.get("X-Envelope")
    query = request.query_params.get("envelope")
    return _truthy(header) or _truthy(query)


def ok_response(
    request: Request | None,
    payload: Any,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if wants_envelope(request):
        body = {"ok": True, "data": payload}
    else:
        body = payload
    return JSONResponse(body, status_code=status_code, headers=headers)


def no_store(response: JSONResponse) -> JSONResponse:
    response.headers["Cache-Control"] = "no-store"
    return response
