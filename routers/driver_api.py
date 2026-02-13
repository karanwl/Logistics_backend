import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from starlette.requests import Request

from auth import (
    clear_session,
    issue_session,
    verify_password,
    get_user_from_token,
    maybe_upgrade_password,
    invalidate_cached_token,
)
from config import settings
from push_notifications import register_push_token, unregister_push_token, send_push_notification, tokens_for_identities
from responses import ok_response
from services import DatabaseService
from utils import business_date_iso, next_plan_date_iso, parse_iso_date


router = APIRouter(prefix="/api/driver", tags=["driver"])
logger = logging.getLogger(__name__)


def _redact_user_record(rec: dict | None) -> dict | None:
    if not rec:
        return rec
    fields = dict(rec.get("fields", {}) or {})
    if settings.USERS_PASSWORD_FIELD in fields:
        fields[settings.USERS_PASSWORD_FIELD] = "<redacted>"
    return {"id": rec.get("id"), "fields": fields}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def _require_iso_date_param(value: str, field_name: str) -> str:
    try:
        return parse_iso_date(value, field_name=field_name, allow_none=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _business_date(offset_days: int = 0) -> str:
    return business_date_iso(offset_days=offset_days)


def _next_plan_date() -> str:
    return next_plan_date_iso(logger=logger)


def _normalize_name(value: str | None) -> str:
    return str(value or "").strip().lower()


def _get_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return ""
    lower = auth.lower()
    if lower.startswith("bearer "):
        return auth[7:].strip()
    if lower.startswith("token "):
        return auth[6:].strip()
    return ""


def _get_current_user_api(request: Request):
    token = _get_bearer_token(request) or request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        return None
    return get_user_from_token(token)


def _role_allows_driver(role: str | None) -> bool:
    role_norm = _normalize_name(role)
    driver_role = _normalize_name(settings.DRIVERS_ROLE_VALUE or "driver")
    return role_norm in {driver_role, "driver", "manager", "admin"}


def _require_driver(request: Request):
    user = _get_current_user_api(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not _role_allows_driver(user.get("role")):
        raise HTTPException(status_code=403, detail="Driver access required.")
    return user


def _parse_json(val: str | None, default):
    try:
        return json.loads(val) if val else default
    except Exception:
        return default


def _normalize_status(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in raw:
        raw = raw.replace("__", "_")
    return raw.strip("_")


def _status_field_value(fields: dict) -> str:
    if settings.DELIVERIES_STATUS_FIELD:
        status = str(fields.get(settings.DELIVERIES_STATUS_FIELD) or "").strip()
        if status:
            return status
    status = str(fields.get("Status") or "").strip()
    if status:
        return status
    notes = str(fields.get(settings.DELIVERIES_DRIVER_NOTES_FIELD) or "").strip()
    if notes.lower().startswith("[status:"):
        closing = notes.find("]")
        if closing > 8:
            candidate = notes[8:closing].strip()
            return _normalize_status(candidate)
    return ""


def _parse_photo_urls(value: object) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                txt = item.strip()
                if txt:
                    out.append(txt)
                continue
            if isinstance(item, dict):
                txt = str(item.get("url") or item.get("href") or "").strip()
                if txt:
                    out.append(txt)
        return out

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return _parse_photo_urls(parsed)
        return [raw]

    return []


def _driver_notes_value(fields: dict) -> str | None:
    raw = str(fields.get(settings.DELIVERIES_DRIVER_NOTES_FIELD) or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("[status:"):
        closing = raw.find("]")
        if closing >= 0:
            text = raw[closing + 1 :].strip()
            return text or None
    return raw


def _as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _as_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _load_published_plan(svc: DatabaseService, plan_date: str):
    record = svc.get_published_plan(plan_date)
    if not record:
        return None
    fields = record.get("fields", {})
    return {
        "record_id": record.get("id"),
        "plan_date": fields.get(settings.PUBLISHED_PLANS_DATE_FIELD) or plan_date,
        "routes": _parse_json(fields.get(settings.PUBLISHED_PLANS_ROUTES_FIELD), []),
        "deliveries": _parse_json(fields.get(settings.PUBLISHED_PLANS_DELIVERIES_FIELD), []),
        "meta": _parse_json(fields.get(settings.PUBLISHED_PLANS_META_FIELD), {}),
        "fleet": _parse_json(fields.get(settings.PUBLISHED_PLANS_FLEET_FIELD), {}),
        "driver_profiles": _parse_json(fields.get(settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD), []),
        "published_by": fields.get(settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD),
        "status": fields.get(settings.PUBLISHED_PLANS_STATUS_FIELD),
        "updated_at": fields.get(settings.PUBLISHED_PLANS_UPDATED_AT_FIELD),
        "created_at": record.get("createdTime"),
    }


def _resolve_plan_date(svc: DatabaseService, requested: str | None) -> str:
    if requested:
        return requested
    today = _business_date()
    if svc.get_published_plan(today):
        return today
    return _next_plan_date()


def _route_driver_name(route: dict) -> str:
    profile = route.get("driver_profile")
    if isinstance(profile, str):
        return profile
    if isinstance(profile, dict):
        return profile.get("name", "")
    driver = route.get("driver")
    if isinstance(driver, str):
        return driver
    return ""


def _driver_identity_candidates(user: dict) -> set[str]:
    return {
        _normalize_name(user.get("display_name")),
        _normalize_name(user.get("name")),
        _normalize_name(user.get("email")),
    }


def _route_matches_driver(route: dict, user: dict) -> bool:
    route_name = _normalize_name(_route_driver_name(route))
    if not route_name:
        return False
    candidates = _driver_identity_candidates(user)
    return route_name in candidates


def _extract_delivery_ids(routes: list[dict]) -> set[str]:
    ids: set[str] = set()
    for route in routes or []:
        stops = route.get("stops")
        if isinstance(stops, list):
            for stop in stops:
                if not isinstance(stop, list):
                    continue
                for item in stop:
                    if isinstance(item, dict) and item.get("type") == "delivery":
                        if item.get("id") is not None:
                            ids.add(str(item.get("id")).strip())
        jobs = route.get("jobs") or []
        if isinstance(jobs, list):
            for stop in jobs:
                if not isinstance(stop, list):
                    continue
                for token in stop:
                    base = str(token or "").split("_Part")[0].strip()
                    if base and base.lower() != "depot":
                        ids.add(base)
    return ids


def _filter_routes_for_driver(routes: list[dict], user: dict, include_all: bool) -> list[dict]:
    if include_all:
        return routes
    return [r for r in routes or [] if _route_matches_driver(r, user)]


def _delivery_assigned_driver(delivery: dict) -> str:
    for key in ("driver", "Driver", "driver_name", "Driver Name", "assigned_driver"):
        val = delivery.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return ""


def _delivery_matches_driver(delivery: dict, user: dict) -> bool:
    assigned = _normalize_name(_delivery_assigned_driver(delivery))
    if not assigned:
        return False
    return assigned in _driver_identity_candidates(user)


def _route_has_driver_assignment(route: dict) -> bool:
    return bool(_normalize_name(_route_driver_name(route)))


def _filter_routes_by_delivery_ids(routes: list[dict], delivery_ids: set[str]) -> list[dict]:
    if not delivery_ids:
        return []
    matched: list[dict] = []
    for route in routes or []:
        route_ids = _extract_delivery_ids([route])
        if route_ids.intersection(delivery_ids):
            matched.append(route)
    return matched


def _filter_plan_for_driver(
    routes: list[dict],
    deliveries: list[dict],
    user: dict,
    include_all: bool,
) -> tuple[list[dict], list[dict]]:
    all_routes = routes or []
    all_deliveries = [d for d in (deliveries or []) if isinstance(d, dict)]

    if include_all:
        all_ids = _extract_delivery_ids(all_routes)
        if not all_ids:
            return all_routes, all_deliveries
        filtered = [d for d in all_deliveries if str(d.get("id") or "").strip() in all_ids]
        return all_routes, filtered

    matched_routes = _filter_routes_for_driver(all_routes, user, include_all=False)
    matched_ids = _extract_delivery_ids(matched_routes)
    matched_deliveries = [
        d for d in all_deliveries
        if str(d.get("id") or "").strip() in matched_ids
    ]
    if matched_routes or matched_deliveries:
        return matched_routes, matched_deliveries

    has_route_assignments = any(_route_has_driver_assignment(route) for route in all_routes)
    if has_route_assignments:
        return matched_routes, matched_deliveries

    # Fallback for legacy/unassigned published routes: use delivery-level driver field.
    delivery_fallback = [d for d in all_deliveries if _delivery_matches_driver(d, user)]
    if not delivery_fallback:
        return matched_routes, matched_deliveries

    fallback_ids = {str(d.get("id") or "").strip() for d in delivery_fallback if str(d.get("id") or "").strip()}
    fallback_routes = _filter_routes_by_delivery_ids(all_routes, fallback_ids)
    return fallback_routes, delivery_fallback


class DriverLoginRequest(BaseModel):
    email: str
    password: str


class DriverLocation(BaseModel):
    lat: float | None = None
    lon: float | None = None


class DriverStatusUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None
    photo_urls: list[str] | None = Field(default_factory=list)
    timestamp: str | None = None
    location: DriverLocation | None = None


class PushRegisterRequest(BaseModel):
    token: str
    platform: str | None = None


class PushUnregisterRequest(BaseModel):
    token: str | None = None


class PushTestRequest(BaseModel):
    title: str | None = None
    body: str | None = None


class DeliveryDetailResponse(BaseModel):
    delivery_id: str
    job_id: str | None = None
    job_name: str | None = None
    job_address: str | None = None
    site_contact: str | None = None
    site_contact_number: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    sales_order: str | None = None
    scheduled_date: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    med_skid: int = 0
    small_skid: int = 0
    large_skid: int = 0
    exp_frame: int = 0
    frame: int = 0
    door: int = 0
    box: int = 0
    pickup: bool = False
    pickup_vehicle: str | None = None
    requires_empty_truck: bool = False
    assigned_driver: str | None = None
    status: str | None = None
    driver_notes: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    status_updated_at: str | None = None


@router.post("/auth/login")
def driver_login(payload: DriverLoginRequest, request: Request):
    if settings.AUTH_DEBUG:
        logger.info("Driver login input email=%s password_len=%s", payload.email, len(payload.password or ""))
    svc = DatabaseService()
    rec = svc.get_user_by_email(payload.email.strip())
    if settings.AUTH_DEBUG:
        logger.info("Driver login DB user record=%s", _redact_user_record(rec))
    if not rec:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    fields = rec.get("fields", {})
    if not fields.get(settings.USERS_ACTIVE_FIELD, True):
        raise HTTPException(status_code=403, detail="Account disabled. Contact manager/admin.")

    stored = fields.get(settings.USERS_PASSWORD_FIELD, "") or ""
    if not verify_password(payload.password, stored):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    maybe_upgrade_password(rec.get("id"), payload.password, stored)

    role = fields.get(settings.USERS_ROLE_FIELD)
    if not _role_allows_driver(role):
        raise HTTPException(status_code=403, detail="Driver access required.")

    session = issue_session(rec.get("id"))
    user = get_user_from_token(session["token"]) or {
        "record_id": rec.get("id"),
        "email": fields.get(settings.USERS_EMAIL_FIELD),
        "name": fields.get(settings.USERS_NAME_FIELD),
        "role": role or "driver",
    }
    return ok_response(
        request,
        {
            "ok": True,
            "token": session["token"],
            "token_type": "Bearer",
            "expires": session["expires"].isoformat(),
            "user": {
                "id": user.get("record_id"),
                "email": user.get("email"),
                "name": user.get("name") or user.get("display_name"),
                "role": user.get("role"),
            },
        },
    )


@router.post("/auth/logout")
def driver_logout(request: Request):
    user = _require_driver(request)
    clear_session(user.get("record_id"))
    return ok_response(request, {"ok": True})


@router.post("/auth/refresh")
def driver_refresh_session(request: Request):
    old_token = _get_bearer_token(request)
    user = _require_driver(request)
    if not user.get("record_id"):
        raise HTTPException(status_code=401, detail="Authentication required.")

    session = issue_session(user.get("record_id"))
    if old_token:
        invalidate_cached_token(old_token)

    payload = {
        "ok": True,
        "token": session["token"],
        "token_type": "Bearer",
        "expires": session["expires"].isoformat(),
        "user": {
            "id": user.get("record_id"),
            "email": user.get("email"),
            "name": user.get("name") or user.get("display_name"),
            "role": user.get("role"),
        },
    }
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/push/register")
def driver_push_register(payload: PushRegisterRequest, request: Request):
    user = _require_driver(request)
    token = str(payload.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Push token is required.")

    identities = {
        user.get("record_id"),
        user.get("email"),
        user.get("name"),
        user.get("display_name"),
    }
    count = 0
    for identity in identities:
        count = max(count, register_push_token(identity, token))

    resp = ok_response(
        request,
        {
            "ok": True,
            "registered": True,
            "device_count": count,
            "platform": (payload.platform or "").strip().lower() or None,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/push/unregister")
def driver_push_unregister(payload: PushUnregisterRequest, request: Request):
    user = _require_driver(request)
    identities = {
        user.get("record_id"),
        user.get("email"),
        user.get("name"),
        user.get("display_name"),
    }
    remaining = 0
    for identity in identities:
        remaining = max(remaining, unregister_push_token(identity, payload.token))

    resp = ok_response(
        request,
        {
            "ok": True,
            "registered": remaining > 0,
            "device_count": remaining,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/push/test")
def driver_push_test(payload: PushTestRequest, request: Request):
    user = _require_driver(request)
    identities = [user.get("record_id"), user.get("email"), user.get("name"), user.get("display_name")]
    tokens = tokens_for_identities(identities)
    result = send_push_notification(
        tokens=tokens,
        title=(payload.title or "Citywide Logistics").strip() or "Citywide Logistics",
        body=(payload.body or "Test push from server").strip() or "Test push from server",
        data={"kind": "test"},
    )
    resp = ok_response(
        request,
        {
            "ok": bool(result.get("ok")),
            "tokens": len(tokens),
            "result": result,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/route/today")
def driver_route_today(request: Request, include_all: bool = False):
    user = _require_driver(request)
    if include_all and not _normalize_name(user.get("role")) in {"admin", "manager"}:
        include_all = False

    svc = DatabaseService()
    plan_date = _resolve_plan_date(svc, None)
    plan = _load_published_plan(svc, plan_date)
    if not plan:
        return ok_response(request, {"published": False, "plan_date": plan_date, "routes": [], "deliveries": []})

    routes, deliveries = _filter_plan_for_driver(
        plan.get("routes", []),
        plan.get("deliveries", []),
        user,
        include_all,
    )

    resp = ok_response(
        request,
        {
            "published": True,
            "plan_date": plan.get("plan_date"),
            "updated_at": plan.get("updated_at") or plan.get("created_at"),
            "routes": routes,
            "deliveries": deliveries,
            "meta": plan.get("meta") or {},
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/route/{plan_date}")
def driver_route_date(plan_date: str, request: Request, include_all: bool = False):
    user = _require_driver(request)
    if include_all and not _normalize_name(user.get("role")) in {"admin", "manager"}:
        include_all = False

    svc = DatabaseService()
    plan_date = _require_iso_date_param(plan_date, "plan_date")
    plan = _load_published_plan(svc, plan_date)
    if not plan:
        return ok_response(request, {"published": False, "plan_date": plan_date, "routes": [], "deliveries": []})

    routes, deliveries = _filter_plan_for_driver(
        plan.get("routes", []),
        plan.get("deliveries", []),
        user,
        include_all,
    )

    resp = ok_response(
        request,
        {
            "published": True,
            "plan_date": plan.get("plan_date"),
            "updated_at": plan.get("updated_at") or plan.get("created_at"),
            "routes": routes,
            "deliveries": deliveries,
            "meta": plan.get("meta") or {},
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/route/changes")
def driver_route_changes(request: Request, since: str | None = None, date: str | None = None):
    user = _require_driver(request)
    svc = DatabaseService()
    plan_date = _require_iso_date_param(date, "date") if date else _resolve_plan_date(svc, date)
    plan = _load_published_plan(svc, plan_date)
    if not plan:
        return ok_response(request, {"published": False, "changed": False, "plan_date": plan_date})

    updated_at = plan.get("updated_at") or plan.get("created_at")
    updated_dt = _parse_iso(updated_at)
    since_dt = _parse_iso(since)
    changed = True
    if updated_dt and since_dt:
        changed = updated_dt > since_dt

    resp = ok_response(
        request,
        {
            "published": True,
            "changed": changed,
            "plan_date": plan.get("plan_date"),
            "updated_at": updated_at,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/route-checks")
def driver_route_changes_v2(request: Request, since: str | None = None, date: str | None = None):
    return driver_route_changes(request=request, since=since, date=date)


@router.get("/deliveries/{delivery_id}")
def driver_delivery_detail(delivery_id: str, request: Request):
    _require_driver(request)
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery id.")

    svc = DatabaseService()
    record = svc.get_delivery_by_business_id(delivery_id)
    if not record:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    fields = record.get("fields", {})
    job_ids = fields.get(settings.DELIVERIES_JOB_LINK_FIELD) or []
    job_id = None
    if isinstance(job_ids, list) and job_ids:
        first = job_ids[0]
        if first is not None and str(first).strip():
            job_id = str(first).strip()

    job_name = None
    job_address = None
    site_contact = None
    site_contact_number = None
    latitude = None
    longitude = None
    if job_id:
        try:
            jobsite = svc.get_record("Jobsites", job_id)
            job_fields = jobsite.get("fields", {}) if isinstance(jobsite, dict) else {}
            job_name = str(job_fields.get(settings.JOBSITES_NAME_FIELD) or "").strip() or None
            job_address = str(job_fields.get(settings.JOBSITES_ADDRESS_FIELD) or "").strip() or None
            site_contact = str(job_fields.get("Site Contact") or "").strip() or None
            site_contact_number = str(job_fields.get("Site Contact Number") or "").strip() or None
            latitude = _as_float(job_fields.get(settings.JOBSITES_LAT_FIELD))
            longitude = _as_float(job_fields.get(settings.JOBSITES_LON_FIELD))
        except Exception:
            logger.warning("Failed to load jobsite for delivery_id=%s job_id=%s", delivery_id, job_id, exc_info=True)

    payload = DeliveryDetailResponse(
        delivery_id=str(record.get("id") or delivery_id),
        job_id=job_id,
        job_name=job_name,
        job_address=job_address,
        site_contact=site_contact,
        site_contact_number=site_contact_number,
        latitude=latitude,
        longitude=longitude,
        sales_order=str(fields.get(settings.DELIVERIES_SALES_ORDER_FIELD) or "").strip() or None,
        scheduled_date=str(fields.get("Scheduled Date") or "").strip() or None,
        window_start=str(fields.get("Window Start") or "").strip() or None,
        window_end=str(fields.get("Window End") or "").strip() or None,
        med_skid=_as_int(fields.get("Med Skid")),
        small_skid=_as_int(fields.get("Small Skid")),
        large_skid=_as_int(fields.get("Large Skid")),
        exp_frame=_as_int(fields.get("Exp Frame")),
        frame=_as_int(fields.get("Frame")),
        door=_as_int(fields.get("Door")),
        box=_as_int(fields.get("Box")),
        pickup=bool(fields.get("Pickup")),
        pickup_vehicle=str(fields.get(settings.DELIVERIES_PICKUP_VEHICLE_FIELD) or "").strip() or None,
        requires_empty_truck=bool(fields.get("Requires Empty Truck")),
        assigned_driver=str(fields.get(settings.DELIVERIES_DRIVER_FIELD) or "").strip() or None,
        status=_status_field_value(fields) or None,
        driver_notes=_driver_notes_value(fields),
        photo_urls=_parse_photo_urls(fields.get(settings.DELIVERIES_PHOTO_URLS_FIELD)),
        status_updated_at=str(fields.get(settings.DELIVERIES_STATUS_UPDATED_FIELD) or "").strip() or None,
    )

    resp = ok_response(request, payload.model_dump())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/deliveries/{delivery_id}/status")
def update_delivery_status(delivery_id: str, payload: DriverStatusUpdate, request: Request):
    user = _require_driver(request)
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery id.")

    allowed_statuses = {"en_route", "arrived", "delivered", "failed"}
    status = _normalize_status(payload.status)
    if not status:
        raise HTTPException(status_code=400, detail="Status is required.")
    if status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid status. Allowed: en_route, arrived, delivered, failed.",
        )
    notes_input = (payload.notes or "").strip()
    if status == "failed" and not notes_input:
        raise HTTPException(status_code=400, detail="Notes are required when status is failed.")
    notes = notes_input
    timestamp = payload.timestamp or _now_iso()
    photo_urls = [str(u).strip() for u in (payload.photo_urls or []) if str(u).strip()]
    loc = payload.location

    fields: dict = {}
    if settings.DELIVERIES_STATUS_FIELD:
        fields[settings.DELIVERIES_STATUS_FIELD] = status
    elif settings.DELIVERIES_DRIVER_NOTES_FIELD:
        # Fallback for deployments without a dedicated status column.
        notes = f"[status:{status}] {notes_input}".strip()
    if notes and settings.DELIVERIES_DRIVER_NOTES_FIELD:
        fields[settings.DELIVERIES_DRIVER_NOTES_FIELD] = notes
    if photo_urls:
        if settings.DELIVERIES_PHOTO_URLS_FIELD:
            if settings.DELIVERIES_PHOTO_ATTACHMENTS:
                fields[settings.DELIVERIES_PHOTO_URLS_FIELD] = [{"url": u} for u in photo_urls]
            else:
                fields[settings.DELIVERIES_PHOTO_URLS_FIELD] = json.dumps(photo_urls, separators=(",", ":"))
    if timestamp and settings.DELIVERIES_STATUS_UPDATED_FIELD:
        fields[settings.DELIVERIES_STATUS_UPDATED_FIELD] = timestamp
    if status.lower() == "arrived" and settings.DELIVERIES_ARRIVED_AT_FIELD:
        fields[settings.DELIVERIES_ARRIVED_AT_FIELD] = timestamp
    if status.lower() == "delivered" and settings.DELIVERIES_DELIVERED_AT_FIELD:
        fields[settings.DELIVERIES_DELIVERED_AT_FIELD] = timestamp
    if loc and loc.lat is not None and settings.DELIVERIES_DRIVER_LAT_FIELD:
        fields[settings.DELIVERIES_DRIVER_LAT_FIELD] = float(loc.lat)
    if loc and loc.lon is not None and settings.DELIVERIES_DRIVER_LON_FIELD:
        fields[settings.DELIVERIES_DRIVER_LON_FIELD] = float(loc.lon)

    if settings.DELIVERIES_DRIVER_FIELD and (user.get("name") or user.get("display_name")):
        fields[settings.DELIVERIES_DRIVER_FIELD] = user.get("name") or user.get("display_name")

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update.")

    svc = DatabaseService()
    updated = svc.update_delivery_by_business_id(delivery_id, fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    resp = ok_response(request, {"ok": True, "delivery_id": delivery_id, "updated_fields": list(fields.keys())})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/uploads/photo")
async def upload_photo(request: Request, file: UploadFile = File(...)):
    _require_driver(request)
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Missing file.")

    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed.")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename)[1].lower()
    if not ext:
        ext = ".jpg" if content_type.endswith("jpeg") else ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    target_path = os.path.join(settings.UPLOAD_DIR, filename)

    max_bytes = max(1, int(settings.UPLOAD_MAX_MB)) * 1024 * 1024
    total = 0
    with open(target_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out.close()
                try:
                    os.remove(target_path)
                except OSError:
                    logger.warning("Failed to remove oversized upload %s", target_path, exc_info=True)
                raise HTTPException(status_code=413, detail="File too large.")
            out.write(chunk)

    url_path = f"/uploads/{filename}"
    resp = ok_response(request, {"ok": True, "url": url_path, "filename": filename, "bytes": total})
    resp.headers["Cache-Control"] = "no-store"
    return resp
