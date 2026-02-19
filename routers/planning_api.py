from fastapi import APIRouter, Depends, Form
import json
import logging

from pydantic import BaseModel, Field, field_validator
from starlette.requests import Request

from fastapi import HTTPException

import requests

from typing import Any, Dict
import uuid
import threading

from auth import get_current_user, hash_password
from idempotency import IdempotencyService
from planning import (
    debug_tomorrow_matrix,
    generate_tomorrow_plan,
    recalculate_manual_plan,
    recalculate_tomorrow_plan,
)
from push_notifications import send_push_notification, tokens_for_identities
from services import DatabaseService, MapboxService
from config import settings
from security import csrf_protect

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from responses import ok_response
from utils import business_date_iso, next_plan_date_iso, parse_iso_date, parse_photo_refs, parse_sales_order, to_int
from routers.driver_api import _photo_urls_for_response

router = APIRouter(dependencies=[Depends(csrf_protect)])
logger = logging.getLogger(__name__)

_solve_jobs_lock = threading.Lock()
_solve_jobs: dict[str, dict[str, Any]] = {}
_DASHBOARD_VEHICLE_TYPES = {"all", "truck", "van"}


def _wants_refresh(request: Request) -> bool:
    refresh = (request.query_params.get("refresh") or "").strip().lower()
    if refresh in {"1", "true", "yes", "y"}:
        return True
    cache_control = (request.headers.get("cache-control") or "").lower()
    pragma = (request.headers.get("pragma") or "").lower()
    return "no-cache" in cache_control or "max-age=0" in cache_control or "no-cache" in pragma


def _idempotency_key(request: Request) -> str:
    return (request.headers.get("x-idempotency-key") or request.headers.get("idempotency-key") or "").strip()


def _begin_idempotent_request(
    request: Request,
    *,
    path: str,
    payload: Any,
):
    key = _idempotency_key(request)
    if not key:
        return None, None
    return IdempotencyService.begin(
        key=key,
        method=request.method.upper(),
        path=path,
        payload=payload,
    )


def _idempotent_replay_response(request: Request, replay) -> Any:
    resp = ok_response(request, replay.body, status_code=replay.status_code)
    resp.headers["X-Idempotency-Replayed"] = "true"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _require_manager_admin(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    return user


def _delivery_photo_field_name() -> str:
    return str(settings.DELIVERIES_PHOTO_URLS_FIELD or "").strip() or "Photo URLs"


def _require_authenticated_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required.")
    return user


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


def _delivery_detail_payload(svc: DatabaseService, rec: dict, fallback_delivery_id: str) -> Dict[str, Any]:
    fields = rec.get("fields", {})
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
            logger.warning(
                "Failed to load jobsite for delivery_id=%s job_id=%s",
                fallback_delivery_id,
                job_id,
                exc_info=True,
            )

    return {
        "delivery_id": str(rec.get("id") or fallback_delivery_id),
        "job_id": job_id,
        "job_name": job_name,
        "job_address": job_address,
        "site_contact": site_contact,
        "site_contact_number": site_contact_number,
        "latitude": latitude,
        "longitude": longitude,
        "sales_order": str(fields.get(settings.DELIVERIES_SALES_ORDER_FIELD) or "").strip() or None,
        "scheduled_date": str(fields.get("Scheduled Date") or "").strip() or None,
        "window_start": str(fields.get("Window Start") or "").strip() or None,
        "window_end": str(fields.get("Window End") or "").strip() or None,
        "med_skid": _as_int(fields.get("Med Skid")),
        "small_skid": _as_int(fields.get("Small Skid")),
        "large_skid": _as_int(fields.get("Large Skid")),
        "exp_frame": _as_int(fields.get("Exp Frame")),
        "frame": _as_int(fields.get("Frame")),
        "door": _as_int(fields.get("Door")),
        "box": _as_int(fields.get("Box")),
        "pickup": bool(fields.get("Pickup")),
        "pickup_vehicle": str(fields.get(settings.DELIVERIES_PICKUP_VEHICLE_FIELD) or "").strip() or None,
        "requires_empty_truck": bool(fields.get("Requires Empty Truck")),
        "assigned_driver": str(fields.get(settings.DELIVERIES_DRIVER_FIELD) or "").strip() or None,
        "status": _status_field_value(fields) or None,
        "driver_notes": _driver_notes_value(fields),
        "photo_urls": _photo_urls_for_response(fields.get(_delivery_photo_field_name())),
        "status_updated_at": str(fields.get(settings.DELIVERIES_STATUS_UPDATED_FIELD) or "").strip() or None,
    }


@router.get("/me")
def current_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required.")
    payload = {
        "id": user.get("record_id") or user.get("id"),
        "email": user.get("email"),
        "name": user.get("name") or user.get("display_name"),
        "role": user.get("role"),
    }
    return ok_response(request, {"user": payload})


class FleetRequest(BaseModel):
    trucks: int = Field(default=settings.PHYSICAL_TRUCKS, ge=0)
    vans: int = Field(default=0, ge=0)


class SolveRunRequest(BaseModel):
    trucks: int = Field(default=settings.PHYSICAL_TRUCKS, ge=0)
    vans: int = Field(default=0, ge=0)
    force_refresh: bool = False
    plan_date: str | None = None

    @field_validator("plan_date")
    @classmethod
    def _validate_plan_date(cls, value):
        return parse_iso_date(value, field_name="plan_date", allow_none=True)


class RecalculateRoute(BaseModel):
    vehicle_id: int | None = None
    vehicle_type: str
    stop_nodes: list[int] | None = None
    nodes: list[int] | None = None


class PublishPlanRequest(BaseModel):
    routes: list[dict]
    deliveries: list[dict]
    meta: dict | None = None
    fleet: dict | None = None
    plan_date: str | None = None
    driver_profiles: list[dict] | None = None

    @field_validator("plan_date")
    @classmethod
    def _validate_plan_date(cls, value):
        return parse_iso_date(value, field_name="plan_date", allow_none=True)


class ManualRecalculateRequest(BaseModel):
    routes: list[dict] = Field(default_factory=list)
    deliveries: list[dict] = Field(default_factory=list)
    meta: dict | None = None


class CustomStopPoint(BaseModel):
    lat: float | None = None
    lon: float | None = None
    address: str | None = None


class CustomStopEstimateRequest(BaseModel):
    prev: CustomStopPoint
    custom: CustomStopPoint
    next: CustomStopPoint
    vehicle_type: str | None = None
    service_minutes: int | None = None


class RescheduleRequest(BaseModel):
    scheduled_date: str | None = None

    @field_validator("scheduled_date")
    @classmethod
    def _validate_scheduled_date(cls, value):
        return parse_iso_date(value, field_name="scheduled_date", allow_none=True)


class PSDeliveredRequest(BaseModel):
    delivered: bool = False


class BulkDeliveryItem(BaseModel):
    jobsite_id: str
    scheduled_date: str
    sales_order: str | None = None
    door: int = Field(default=0, ge=0)
    frame: int = Field(default=0, ge=0)
    med_skid: int = Field(default=0, ge=0)
    large_skid: int = Field(default=0, ge=0)
    small_skid: int = Field(default=0, ge=0)
    box: int = Field(default=0, ge=0)
    exp_frame: int = Field(default=0, ge=0)
    pickup: bool = False
    pickup_vehicle: str | None = None
    requires_empty_truck: bool = False
    window_start: str | None = None
    window_end: str | None = None

    @field_validator("scheduled_date")
    @classmethod
    def _validate_scheduled_date(cls, value):
        return parse_iso_date(value, field_name="scheduled_date", allow_none=False)


class BulkCreateDeliveriesRequest(BaseModel):
    deliveries: list[BulkDeliveryItem] = Field(default_factory=list)


class BulkCargoUpdateRequest(BaseModel):
    med_skid: int | None = Field(default=None, ge=0)
    large_skid: int | None = Field(default=None, ge=0)
    small_skid: int | None = Field(default=None, ge=0)
    door: int | None = Field(default=None, ge=0)
    frame: int | None = Field(default=None, ge=0)
    exp_frame: int | None = Field(default=None, ge=0)
    box: int | None = Field(default=None, ge=0)
    pickup: bool | None = None
    pickup_vehicle: str | None = None
    requires_empty_truck: bool | None = None


class BulkUpdateDeliveryItem(BaseModel):
    delivery_id: str
    ps_delivered: bool | None = None
    scheduled_date: str | None = None
    med_skid: int | None = Field(default=None, ge=0)
    large_skid: int | None = Field(default=None, ge=0)
    small_skid: int | None = Field(default=None, ge=0)
    door: int | None = Field(default=None, ge=0)
    frame: int | None = Field(default=None, ge=0)
    exp_frame: int | None = Field(default=None, ge=0)
    box: int | None = Field(default=None, ge=0)
    window_start: str | None = None
    window_end: str | None = None
    pickup: bool | None = None
    pickup_vehicle: str | None = None
    requires_empty_truck: bool | None = None

    @field_validator("scheduled_date")
    @classmethod
    def _validate_scheduled_date(cls, value):
        return parse_iso_date(value, field_name="scheduled_date", allow_none=True, allow_empty=True)


class BulkUpdateDeliveriesRequest(BaseModel):
    deliveries: list[BulkUpdateDeliveryItem] = Field(default_factory=list)


@router.get("/drivers/list")
def drivers_list(request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    records = svc.list_users_min(role=settings.DRIVERS_ROLE_VALUE, active_only=False, force_refresh=_wants_refresh(request))
    drivers = [d.get("display_name") or d.get("name") for d in records if d.get("display_name") or d.get("name")]
    resp = ok_response(request, {"drivers": drivers})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/drivers/list-full")
def drivers_list_full(request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    records = svc.list_users_min(role=settings.DRIVERS_ROLE_VALUE, active_only=False, force_refresh=_wants_refresh(request))
    drivers = []
    for rec in records or []:
        drivers.append(
            {
                "id": rec.get("record_id") or rec.get("id"),
                "email": rec.get("email"),
                "name": rec.get("name") or rec.get("display_name"),
                "role": rec.get("role"),
            }
        )
    resp = ok_response(request, {"drivers": drivers})
    resp.headers["Cache-Control"] = "no-store"
    return resp



@router.get("/deliveries/all")
def deliveries_all(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Login required.")
    svc = DatabaseService()
    deliveries = svc.list_deliveries_min(force_refresh=_wants_refresh(request))
    resp = ok_response(request, {"deliveries": deliveries, "count": len(deliveries)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/live/today")
def live_today(request: Request, date: str | None = None):
    _require_manager_admin(request)
    live_date = _require_iso_date_param(date, "date") if date else _business_date(offset_days=0)
    svc = DatabaseService()
    payload = svc.get_live_progress_snapshot(live_date=live_date, force_refresh=_wants_refresh(request))
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/tomorrow")
def plan_tomorrow(fleet: FleetRequest, request: Request):
    _require_manager_admin(request)
    force_refresh = _wants_refresh(request)
    resp = ok_response(
        request,
        generate_tomorrow_plan(trucks=fleet.trucks, vans=fleet.vans, force_refresh=force_refresh),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/recalculate")
def plan_recalculate(payload: list[RecalculateRoute], request: Request):
    _require_manager_admin(request)
    routes = [r.model_dump() for r in payload]
    force_refresh = _wants_refresh(request)
    resp = ok_response(request, recalculate_tomorrow_plan(routes, force_refresh=force_refresh))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/manual/recalculate")
def plan_manual_recalculate(payload: ManualRecalculateRequest, request: Request):
    _require_manager_admin(request)
    force_refresh = _wants_refresh(request)
    meta = payload.meta or {}
    depot_lat = meta.get("depot_lat") if isinstance(meta, dict) else None
    depot_lon = meta.get("depot_lon") if isinstance(meta, dict) else None
    try:
        result = recalculate_manual_plan(
            routes=payload.routes or [],
            deliveries=payload.deliveries or [],
            depot_lat=depot_lat,
            depot_lon=depot_lon,
            force_refresh=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    resp = ok_response(request, result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _business_date(offset_days: int = 0) -> str:
    return business_date_iso(offset_days=offset_days)


def _next_plan_date() -> str:
    return next_plan_date_iso(logger=logger)


def _to_int_safe(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float_safe(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_iso_date_param(value: str | None, field_name: str) -> str:
    try:
        return parse_iso_date(value, field_name=field_name, allow_none=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _normalize_plan_delivery(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    d = dict(item)
    raw_id = d.get("id") or d.get("delivery_id") or d.get("record_id")
    if raw_id not in (None, ""):
        d["id"] = str(raw_id).strip()
    if d.get("id"):
        d["delivery_id"] = str(d.get("delivery_id") or d.get("id")).strip()

    if "med_skid" not in d and "skid" in d:
        d["med_skid"] = d.get("skid")
    for field in ("med_skid", "large_skid", "small_skid", "door", "frame", "exp_frame", "box"):
        d[field] = _to_int_safe(d.get(field), 0)
    if "pickup" in d:
        d["pickup"] = bool(d.get("pickup"))
    if "requires_empty_truck" in d:
        d["requires_empty_truck"] = bool(d.get("requires_empty_truck"))
    if "pickup_vehicle" in d or "pickup" in d:
        pickup_flag = bool(d.get("pickup"))
        pickup_vehicle = DatabaseService._normalize_pickup_vehicle(d.get("pickup_vehicle"))
        if pickup_flag and not pickup_vehicle:
            pickup_vehicle = "both"
        if not pickup_flag:
            pickup_vehicle = ""
        d["pickup_vehicle"] = pickup_vehicle

    if "requires_truck" not in d:
        d["requires_truck"] = any(
            d.get(field, 0) > 0 for field in ("med_skid", "large_skid", "small_skid", "door", "frame", "exp_frame")
        )
    return d


def _has_valid_coordinates(item: Dict[str, Any]) -> bool:
    lat = _to_float_safe(item.get("lat"))
    lon = _to_float_safe(item.get("lon"))
    if lat is None or lon is None:
        return False
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return False
    return True


def _enrich_plan_delivery_coordinates(
    svc: DatabaseService,
    plan_date: str,
    deliveries: list[dict],
    force_refresh: bool,
) -> list[dict]:
    if not deliveries:
        return deliveries
    missing_ids = {
        str(item.get("id") or item.get("delivery_id") or "").strip()
        for item in deliveries
        if isinstance(item, dict) and not _has_valid_coordinates(item)
    }
    missing_ids.discard("")
    if not missing_ids:
        return deliveries

    base = svc.build_delivery_plan_payload(plan_date, force_refresh=force_refresh)
    base_lookup: Dict[str, Dict[str, Any]] = {}
    for item in base.get("deliveries") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("delivery_id") or "").strip()
        if key:
            base_lookup[key] = item

    enriched: list[dict] = []
    for item in deliveries:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        key = str(item.get("id") or item.get("delivery_id") or "").strip()
        if key and key in missing_ids and key in base_lookup:
            base_item = base_lookup[key]
            next_item = dict(item)
            next_item["lat"] = _to_float_safe(base_item.get("lat"))
            next_item["lon"] = _to_float_safe(base_item.get("lon"))
            enriched.append(next_item)
            continue
        enriched.append(item)
    return enriched


def _text_or_empty(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _normalize_driver_profile_item(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    profile: Dict[str, Any] = {}
    profile_id = _text_or_empty(item.get("id") or item.get("record_id") or item.get("user_id"))
    email = _text_or_empty(item.get("email"))
    name = _text_or_empty(item.get("name") or item.get("display_name"))
    role = _text_or_empty(item.get("role"))
    if profile_id:
        profile["id"] = profile_id
    if email:
        profile["email"] = email
    if name:
        profile["name"] = name
    if role:
        profile["role"] = role
    return profile


def _driver_profile_candidates(profile: Dict[str, Any]) -> set[str]:
    return {
        _text_or_empty(profile.get("id")).lower(),
        _text_or_empty(profile.get("email")).lower(),
        _text_or_empty(profile.get("name")).lower(),
    } - {""}


def _build_driver_profile_lookup(payload_profiles: list[dict], svc: DatabaseService) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}

    def _register(profile_like: Any):
        normalized = _normalize_driver_profile_item(profile_like)
        if not normalized:
            return
        for key in _driver_profile_candidates(normalized):
            lookup[key] = normalized

    for item in payload_profiles or []:
        _register(item)

    try:
        drivers = svc.list_users_min(role=settings.DRIVERS_ROLE_VALUE, active_only=False, force_refresh=False)
    except Exception:
        drivers = []
    for rec in drivers or []:
        _register(
            {
                "id": rec.get("record_id") or rec.get("id"),
                "email": rec.get("email"),
                "name": rec.get("name") or rec.get("display_name"),
                "role": rec.get("role"),
            }
        )
    return lookup


def _driver_profile_from_assignment(assignment: Any, lookup: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(assignment, dict):
        normalized = _normalize_driver_profile_item(assignment)
        if normalized:
            for key in _driver_profile_candidates(normalized):
                match = lookup.get(key)
                if match:
                    return dict(match)
            return normalized
        assignment = assignment.get("value") or assignment.get("name") or assignment.get("email") or assignment.get("id")

    raw = _text_or_empty(assignment)
    if not raw:
        return {}

    key = raw.lower()
    if key in lookup:
        return dict(lookup[key])

    if "@" in raw:
        return {"email": raw, "name": raw}
    return {"name": raw}


def _route_vehicle_key(route: Dict[str, Any]) -> str:
    for key in ("vehicle_id", "v_id", "vehicleId", "id"):
        if key not in route:
            continue
        value = route.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return _text_or_empty(value)
    return ""


def _normalize_route_driver_profiles(
    routes: list[dict],
    meta: dict | None,
    driver_profiles: list[dict] | None,
    svc: DatabaseService,
) -> tuple[list[dict], list[dict]]:
    assignments_raw = (meta or {}).get("driver_assignments") if isinstance(meta, dict) else {}
    assignments: Dict[str, Any] = {}
    if isinstance(assignments_raw, dict):
        for key, value in assignments_raw.items():
            route_key = _text_or_empty(key)
            if route_key:
                assignments[route_key] = value

    lookup = _build_driver_profile_lookup(driver_profiles or [], svc)
    collected: Dict[tuple[str, str, str], Dict[str, Any]] = {}

    def _collect(profile_like: Any):
        normalized = _normalize_driver_profile_item(profile_like)
        if not normalized:
            return
        signature = (
            _text_or_empty(normalized.get("id")),
            _text_or_empty(normalized.get("email")),
            _text_or_empty(normalized.get("name")),
        )
        collected[signature] = normalized

    normalized_routes: list[dict] = []
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        item = dict(route)

        resolved_profile: Dict[str, Any] = {}
        if isinstance(item.get("driver_profile"), dict):
            resolved_profile = _normalize_driver_profile_item(item.get("driver_profile"))
        elif isinstance(item.get("driver_profile"), str):
            resolved_profile = _driver_profile_from_assignment(item.get("driver_profile"), lookup)

        if not resolved_profile:
            assigned = assignments.get(_route_vehicle_key(item))
            resolved_profile = _driver_profile_from_assignment(assigned, lookup)

        if resolved_profile:
            item["driver_profile"] = resolved_profile
            if not _text_or_empty(item.get("driver")):
                item["driver"] = resolved_profile.get("name") or resolved_profile.get("email") or ""
            _collect(resolved_profile)

        normalized_routes.append(item)

    for profile in driver_profiles or []:
        _collect(profile)

    normalized_driver_profiles = list(collected.values())
    return normalized_routes, normalized_driver_profiles


def _driver_identities_from_routes(routes: list[dict]) -> list[str]:
    identities: set[str] = set()
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        profile = route.get("driver_profile")
        if isinstance(profile, dict):
            for key in ("id", "email", "name"):
                value = profile.get(key)
                if value is not None and str(value).strip():
                    identities.add(str(value).strip())
        driver = route.get("driver")
        if driver is not None and str(driver).strip():
            identities.add(str(driver).strip())
    return sorted(identities)


def _route_metrics_summary(routes: list[dict]) -> dict[str, int]:
    total_distance_m = 0
    total_time_s = 0
    total_stops = 0
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        total_distance_m += _to_int_safe(route.get("total_distance_m"), 0)
        total_time_s += _to_int_safe(route.get("total_time_s"), 0)
        stops = route.get("stops")
        if isinstance(stops, list):
            total_stops += len(stops)
    return {
        "route_count": len([r for r in routes or [] if isinstance(r, dict)]),
        "total_distance_m": int(total_distance_m),
        "total_time_s": int(total_time_s),
        "total_stops": int(total_stops),
    }


def _route_vehicle_type(route: dict) -> str:
    raw = _text_or_empty(
        route.get("vehicle_type") or route.get("v_type") or route.get("vehicleType")
    ).lower()
    return "van" if raw == "van" else "truck"


def _delivery_id_from_stop_item(item: Any) -> str:
    if isinstance(item, dict):
        stop_type = _text_or_empty(item.get("type")).lower()
        if stop_type == "depot":
            return ""
        delivery_id = _text_or_empty(item.get("id") or item.get("delivery_id"))
        if stop_type == "delivery":
            return delivery_id
        if delivery_id:
            return delivery_id
        label = _text_or_empty(item.get("label")).lower()
        if label in {"depot", "depot - reload"}:
            return ""
        return ""
    token = _text_or_empty(item)
    if not token:
        return ""
    lowered = token.lower()
    if lowered in {"depot", "depot - reload"}:
        return ""
    return token.split("_Part", 1)[0].strip()


def _empty_dashboard_metrics() -> Dict[str, int]:
    return {
        "route_count": 0,
        "vehicle_count": 0,
        "delivery_count": 0,
        "total_distance_m": 0,
        "total_time_s": 0,
        "total_stops": 0,
        "delivery_stops": 0,
    }


def _sum_dashboard_metrics(base: dict[str, int], add: dict[str, int]) -> dict[str, int]:
    out = _empty_dashboard_metrics()
    for key in out.keys():
        out[key] = int(_to_int_safe(base.get(key), 0) + _to_int_safe(add.get(key), 0))
    return out


def _delta_dashboard_metrics(
    manual_metrics: dict[str, int],
    published_metrics: dict[str, int],
) -> dict[str, int]:
    out = _empty_dashboard_metrics()
    for key in out.keys():
        out[key] = int(_to_int_safe(published_metrics.get(key), 0) - _to_int_safe(manual_metrics.get(key), 0))
    return out


def _plan_dashboard_metrics(routes: list[dict], vehicle_type: str = "all") -> dict[str, int]:
    out = _empty_dashboard_metrics()
    if vehicle_type not in _DASHBOARD_VEHICLE_TYPES:
        vehicle_type = "all"

    unique_delivery_ids: set[str] = set()
    filtered_routes: list[dict] = []
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        route_vehicle_type = _route_vehicle_type(route)
        if vehicle_type != "all" and route_vehicle_type != vehicle_type:
            continue
        filtered_routes.append(route)

    out["route_count"] = len(filtered_routes)
    out["vehicle_count"] = len(filtered_routes)

    total_distance_m = 0
    total_time_s = 0
    total_stops = 0
    delivery_stops = 0

    for route in filtered_routes:
        total_distance_m += _to_int_safe(route.get("total_distance_m"), 0)
        total_time_s += _to_int_safe(route.get("total_time_s"), 0)
        stops = route.get("stops")
        if not isinstance(stops, list):
            continue
        total_stops += len(stops)
        for stop in stops:
            stop_group = stop if isinstance(stop, list) else [stop]
            group_has_delivery = False
            for stop_item in stop_group:
                delivery_id = _delivery_id_from_stop_item(stop_item)
                if not delivery_id:
                    continue
                group_has_delivery = True
                unique_delivery_ids.add(delivery_id)
            if group_has_delivery:
                delivery_stops += 1

    out["total_distance_m"] = int(total_distance_m)
    out["total_time_s"] = int(total_time_s)
    out["total_stops"] = int(total_stops)
    out["delivery_stops"] = int(delivery_stops)
    out["delivery_count"] = len(unique_delivery_ids)
    return out


def _safe_pct_delta(value: int, base: int) -> float:
    if base <= 0:
        return 0.0
    return round((float(value) / float(base)) * 100.0, 2)


def _dashboard_improvements(
    manual_metrics: dict[str, int],
    published_metrics: dict[str, int],
    compared_days: int,
) -> dict[str, Any]:
    distance_saved_m = int(_to_int_safe(manual_metrics.get("total_distance_m"), 0) - _to_int_safe(published_metrics.get("total_distance_m"), 0))
    time_saved_s = int(_to_int_safe(manual_metrics.get("total_time_s"), 0) - _to_int_safe(published_metrics.get("total_time_s"), 0))
    routes_saved = int(_to_int_safe(manual_metrics.get("route_count"), 0) - _to_int_safe(published_metrics.get("route_count"), 0))
    vehicles_saved = int(_to_int_safe(manual_metrics.get("vehicle_count"), 0) - _to_int_safe(published_metrics.get("vehicle_count"), 0))

    manual_distance = int(_to_int_safe(manual_metrics.get("total_distance_m"), 0))
    manual_time = int(_to_int_safe(manual_metrics.get("total_time_s"), 0))
    day_divisor = max(1, _to_int_safe(compared_days, 0))

    return {
        "distance_saved_m": distance_saved_m,
        "distance_saved_pct": _safe_pct_delta(distance_saved_m, manual_distance),
        "time_saved_s": time_saved_s,
        "time_saved_pct": _safe_pct_delta(time_saved_s, manual_time),
        "routes_saved": routes_saved,
        "vehicles_saved": vehicles_saved,
        "daily_distance_saved_m": int(round(distance_saved_m / day_divisor)),
        "daily_time_saved_s": int(round(time_saved_s / day_divisor)),
    }


def _dashboard_highlights(improvements: dict[str, Any], compared_days: int) -> list[str]:
    if compared_days <= 0:
        return ["No overlapping manual and published plans were found in the selected timeframe."]

    distance_saved_m = _to_int_safe(improvements.get("distance_saved_m"), 0)
    time_saved_s = _to_int_safe(improvements.get("time_saved_s"), 0)
    routes_saved = _to_int_safe(improvements.get("routes_saved"), 0)

    distance_km = abs(distance_saved_m) / 1000.0
    distance_pct = float(improvements.get("distance_saved_pct") or 0.0)
    time_hours = abs(time_saved_s) / 3600.0
    time_pct = float(improvements.get("time_saved_pct") or 0.0)

    highlights: list[str] = []
    if distance_saved_m >= 0:
        highlights.append(
            f"Distance reduced by {distance_km:.1f} km ({distance_pct:.2f}%) over {compared_days} compared day(s)."
        )
    else:
        highlights.append(
            f"Distance increased by {distance_km:.1f} km ({abs(distance_pct):.2f}%) over {compared_days} compared day(s)."
        )

    if time_saved_s >= 0:
        highlights.append(
            f"Route time reduced by {time_hours:.1f} hours ({time_pct:.2f}%)."
        )
    else:
        highlights.append(
            f"Route time increased by {time_hours:.1f} hours ({abs(time_pct):.2f}%)."
        )

    if routes_saved >= 0:
        highlights.append(f"Used {routes_saved} fewer route(s) in total across the timeframe.")
    else:
        highlights.append(f"Used {abs(routes_saved)} more route(s) in total across the timeframe.")
    return highlights


def _capacity_profile_payload() -> Dict[str, int]:
    return {
        "vehicle_capacity_units": int(settings.VEHICLE_CAPACITY_UNITS),
        "med_skid_units": int(settings.MED_SKID_UNITS),
        "large_skid_units": int(settings.LARGE_SKID_UNITS),
        "small_skid_units": int(settings.SMALL_SKID_UNITS),
        "frame_units": int(settings.FRAME_UNITS),
    }


def _solver_penalties_payload() -> Dict[str, Any]:
    return {
        "truck_cost_factor": settings.SOLVER_TRUCK_COST_FACTOR,
        "truck_time_factor": settings.SOLVER_TRUCK_TIME_FACTOR,
        "truck_serve_van_eligible_penalty": settings.SOLVER_TRUCK_SERVE_VAN_ELIGIBLE_PENALTY,
        "time_span_cost_coeff": settings.SOLVER_TIME_SPAN_COST_COEFFICIENT,
        "downtown_soft_latest_s": settings.SOLVER_DOWNTOWN_SOFT_LATEST_SECONDS,
        "downtown_hard_latest_s": settings.SOLVER_DOWNTOWN_HARD_LATEST_SECONDS,
        "downtown_late_penalty_per_sec": settings.SOLVER_DOWNTOWN_LATE_PENALTY_PER_SECOND,
        "van_fixed_cost": settings.SOLVER_VAN_FIXED_COST,
        "truck_fixed_cost_physical": settings.SOLVER_TRUCK_FIXED_COST_PHYSICAL,
        "truck_fixed_cost_overflow": settings.SOLVER_TRUCK_FIXED_COST_OVERFLOW,
        "drop_node_penalty": settings.SOLVER_DROP_NODE_PENALTY,
        "shift_seconds": settings.SOLVER_SHIFT_SECONDS,
        "waiting_slack_seconds": settings.SOLVER_WAITING_SLACK_SECONDS,
        "search_time_limit_s": settings.SOLVER_TIME_LIMIT_SECONDS,
    }


def _canonical_plan_delivery(item: Any) -> Dict[str, Any]:
    normalized = _normalize_plan_delivery(item)
    delivery_id = _text_or_empty(
        normalized.get("id") or normalized.get("delivery_id") or normalized.get("record_id")
    )
    if not delivery_id:
        return {}

    med_skid = _to_int_safe(normalized.get("med_skid"), 0)
    large_skid = _to_int_safe(normalized.get("large_skid"), 0)
    small_skid = _to_int_safe(normalized.get("small_skid"), 0)
    door = _to_int_safe(normalized.get("door"), 0)
    frame = _to_int_safe(normalized.get("frame"), 0)
    exp_frame = _to_int_safe(normalized.get("exp_frame"), 0)
    box = _to_int_safe(normalized.get("box"), 0)

    pickup = bool(normalized.get("pickup"))
    pickup_vehicle = DatabaseService._normalize_pickup_vehicle(normalized.get("pickup_vehicle"))
    if pickup and not pickup_vehicle:
        pickup_vehicle = "both"
    if not pickup:
        pickup_vehicle = ""
    requires_empty_truck = bool(normalized.get("requires_empty_truck"))

    capacity = _capacity_profile_payload()
    computed_units = (
        med_skid * int(capacity["med_skid_units"])
        + large_skid * int(capacity["large_skid_units"])
        + small_skid * int(capacity["small_skid_units"])
        + (frame + exp_frame) * int(capacity["frame_units"])
    )
    capacity_units = _to_int_safe(normalized.get("capacity_units"), computed_units)

    requires_truck = bool(normalized.get("requires_truck")) or bool(
        door > 0
        or frame > 0
        or exp_frame > 0
        or med_skid > 0
        or small_skid > 0
        or large_skid > 0
        or (pickup and (pickup_vehicle == "truck" or requires_empty_truck))
    )

    driver = _text_or_empty(normalized.get("driver"))
    if driver.isdigit():
        driver = ""

    return {
        "id": delivery_id,
        "job_name": _text_or_empty(normalized.get("job_name")),
        "job_address": _text_or_empty(normalized.get("job_address")),
        "sales_order": _text_or_empty(normalized.get("sales_order")),
        "packing_slip": _text_or_empty(normalized.get("packing_slip")),
        "driver": driver,
        "lat": float(_to_float_safe(normalized.get("lat")) or 0.0),
        "lon": float(_to_float_safe(normalized.get("lon")) or 0.0),
        "door": door,
        "frame": frame,
        "exp_frame": exp_frame,
        "med_skid": med_skid,
        "small_skid": small_skid,
        "large_skid": large_skid,
        "box": box,
        "requires_truck": requires_truck,
        "pickup": pickup,
        "pickup_vehicle": pickup_vehicle,
        "requires_empty_truck": requires_empty_truck,
        "window_start": _text_or_empty(normalized.get("window_start")) or None,
        "window_end": _text_or_empty(normalized.get("window_end")) or None,
        "capacity_units": capacity_units,
    }


def _canonical_plan_deliveries(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        canonical = _canonical_plan_delivery(item)
        if canonical:
            out.append(canonical)
    return out


def _matrix_node_count_for_deliveries(deliveries: list[dict]) -> int:
    coords: set[tuple[float, float]] = set()
    for item in deliveries or []:
        if not isinstance(item, dict):
            continue
        lat = _to_float_safe(item.get("lat"))
        lon = _to_float_safe(item.get("lon"))
        if lat is None or lon is None:
            continue
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            continue
        coords.add((round(float(lon), 6), round(float(lat), 6)))
    return 1 + len(coords)


def _missing_delivery_coordinate_ids(deliveries: list[dict]) -> list[str]:
    missing: list[str] = []
    for item in deliveries or []:
        if not isinstance(item, dict):
            continue
        delivery_id = _text_or_empty(item.get("id") or item.get("delivery_id"))
        if not delivery_id:
            continue
        lat = _to_float_safe(item.get("lat"))
        lon = _to_float_safe(item.get("lon"))
        if lat is None or lon is None or (abs(lat) < 1e-9 and abs(lon) < 1e-9):
            missing.append(delivery_id)
    return sorted(set(missing))


def _coerce_fleet_payload(fleet: dict | None, routes: list[dict]) -> Dict[str, int]:
    trucks = _to_int_safe((fleet or {}).get("trucks") if isinstance(fleet, dict) else None, -1)
    vans = _to_int_safe((fleet or {}).get("vans") if isinstance(fleet, dict) else None, -1)
    if trucks >= 0 and vans >= 0:
        return {"trucks": int(max(0, trucks)), "vans": int(max(0, vans))}

    trucks = 0
    vans = 0
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        v_type = _text_or_empty(
            route.get("vehicle_type") or route.get("v_type") or route.get("vehicleType")
        ).lower()
        if v_type == "van":
            vans += 1
        elif v_type == "truck":
            trucks += 1
    return {"trucks": int(trucks), "vans": int(vans)}


def _build_manual_meta_payload(
    meta_base: dict | None,
    *,
    plan_date: str,
    deliveries: list[dict],
    routes: list[dict],
    fleet: dict | None,
    summary: dict | None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(meta_base or {})

    depot_lat = _to_float_safe(payload.get("depot_lat"))
    depot_lon = _to_float_safe(payload.get("depot_lon"))
    if depot_lat is None:
        depot_lat = float(settings.DEPOT_LAT)
    if depot_lon is None:
        depot_lon = float(settings.DEPOT_LON)

    summary_obj = summary if isinstance(summary, dict) else {}
    fleet_payload = _coerce_fleet_payload(fleet if isinstance(fleet, dict) else None, routes)
    matrix_node_count = _to_int_safe(
        summary_obj.get("matrix_node_count"),
        _matrix_node_count_for_deliveries(deliveries),
    )
    delivery_nodes = _to_int_safe(summary_obj.get("delivery_nodes"), max(0, matrix_node_count - 1))
    driver_assignments = payload.get("driver_assignments")
    if not isinstance(driver_assignments, dict):
        driver_assignments = {}

    vehicle_count = int(fleet_payload.get("trucks", 0) + fleet_payload.get("vans", 0))
    if vehicle_count <= 0:
        vehicle_count = len([r for r in routes or [] if isinstance(r, dict)])

    payload.update(
        {
            "plan_date": plan_date,
            "depot_lat": float(depot_lat),
            "depot_lon": float(depot_lon),
            "delivery_count": len(deliveries or []),
            "node_count": int(matrix_node_count),
            "vehicle_count": int(vehicle_count),
            "fleet": fleet_payload,
            "capacity": _capacity_profile_payload(),
            "solver_penalties": _solver_penalties_payload(),
            "driver_assignments": driver_assignments,
        }
    )
    return payload


def _normalize_route_stop_item(item: Any) -> Dict[str, Any] | None:
    if isinstance(item, dict):
        out = dict(item)
        raw_type = _text_or_empty(out.get("type")).lower()
        if raw_type not in {"delivery", "depot"}:
            label = _text_or_empty(out.get("label")).lower()
            if label in {"depot", "depot - reload"}:
                raw_type = "depot"
            elif _text_or_empty(out.get("id") or out.get("delivery_id")):
                raw_type = "delivery"

        if raw_type == "depot":
            label = _text_or_empty(out.get("label")) or "Depot"
            lowered = label.strip().lower()
            if lowered == "depot - reload":
                label = "Depot - Reload"
            elif lowered == "depot":
                label = "Depot"
            out["type"] = "depot"
            out["label"] = label
            return out

        if raw_type == "delivery":
            delivery_id = _text_or_empty(out.get("id") or out.get("delivery_id"))
            out["type"] = "delivery"
            if delivery_id:
                out["id"] = delivery_id
                out["delivery_id"] = delivery_id
            return out
        return None

    token = _text_or_empty(item)
    if not token:
        return None
    lowered = token.lower()
    if lowered in {"depot", "depot - reload"}:
        return {
            "type": "depot",
            "label": "Depot - Reload" if lowered == "depot - reload" else "Depot",
        }
    delivery_id = token.split("_Part", 1)[0].strip()
    if not delivery_id:
        return None
    return {"type": "delivery", "id": delivery_id, "delivery_id": delivery_id}


def _is_depot_stop_group(stop_group: list[Any]) -> bool:
    for item in stop_group:
        if isinstance(item, dict):
            if _text_or_empty(item.get("type")).lower() == "depot":
                return True
            label = _text_or_empty(item.get("label")).lower()
            if label in {"depot", "depot - reload"}:
                return True
        else:
            token = _text_or_empty(item).lower()
            if token in {"depot", "depot - reload"}:
                return True
    return False


def _normalize_route_stops(raw_stops: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(raw_stops, list):
        return []
    normalized: list[list[dict[str, Any]]] = []
    for stop in raw_stops:
        group_items = stop if isinstance(stop, list) else [stop]
        group: list[dict[str, Any]] = []
        for item in group_items:
            parsed = _normalize_route_stop_item(item)
            if parsed:
                group.append(parsed)
        if group:
            normalized.append(group)
    return normalized


def _normalize_manual_route_shape(route: dict) -> dict:
    item = dict(route or {})

    vehicle_id = item.get("vehicle_id")
    if vehicle_id in (None, ""):
        for key in ("v_id", "vehicleId", "id"):
            candidate = item.get(key)
            if candidate not in (None, ""):
                vehicle_id = candidate
                break
    if vehicle_id not in (None, ""):
        item["vehicle_id"] = vehicle_id

    vehicle_type = (
        _text_or_empty(item.get("vehicle_type"))
        or _text_or_empty(item.get("v_type"))
        or _text_or_empty(item.get("vehicleType"))
        or "truck"
    )
    item["vehicle_type"] = vehicle_type.lower()

    stops = _normalize_route_stops(item.get("stops"))
    if not stops and isinstance(item.get("jobs"), list):
        stops = _normalize_route_stops(item.get("jobs"))

    if stops:
        if not _is_depot_stop_group(stops[0]):
            stops = [[{"type": "depot", "label": "Depot"}], *stops]
        if not _is_depot_stop_group(stops[-1]):
            stops = [*stops, [{"type": "depot", "label": "Depot"}]]
    item["stops"] = stops
    return item


def _normalize_manual_routes_shape(routes: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for route in routes or []:
        if isinstance(route, dict):
            normalized.append(_normalize_manual_route_shape(route))
    return normalized


def _build_manual_seed_payload(svc: DatabaseService, plan_date: str, force_refresh: bool) -> Dict[str, Any]:
    base = svc.build_delivery_plan_payload(plan_date, force_refresh=force_refresh)
    deliveries = _canonical_plan_deliveries([_normalize_plan_delivery(d) for d in (base.get("deliveries") or [])])
    summary = {
        "route_count": 0,
        "total_distance_m": 0,
        "total_time_s": 0,
        "total_stops": 0,
        "delivery_nodes": max(0, _matrix_node_count_for_deliveries(deliveries) - 1),
        "matrix_node_count": _matrix_node_count_for_deliveries(deliveries),
    }
    meta_payload = _build_manual_meta_payload(
        {},
        plan_date=plan_date,
        deliveries=deliveries,
        routes=[],
        fleet={"trucks": 0, "vans": 0},
        summary=summary,
    )
    return {
        "plan_date": plan_date,
        "plan_type": "manual",
        "routes": [],
        "deliveries": deliveries,
        "meta": meta_payload,
        "fleet": {"trucks": 0, "vans": 0},
        "driver_profiles": [],
        "status": "manual",
        "updated_at": None,
        "updated_by": None,
    }


@router.post("/plan/publish")
def plan_publish(payload: PublishPlanRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    plan_date = payload.plan_date or _next_plan_date()

    svc = DatabaseService()
    existing_plan = svc.get_plan_payload(plan_date, "published")
    is_update = existing_plan is not None
    normalized_routes, normalized_driver_profiles = _normalize_route_driver_profiles(
        routes=payload.routes or [],
        meta=payload.meta or {},
        driver_profiles=payload.driver_profiles or [],
        svc=svc,
    )
    idem_context, replay = _begin_idempotent_request(
        request,
        path="/plan/publish",
        payload={
            "plan_date": plan_date,
            "routes": normalized_routes,
            "deliveries": payload.deliveries or [],
            "meta": payload.meta or {},
            "fleet": payload.fleet or {},
            "driver_profiles": normalized_driver_profiles,
            "action": "publish",
        },
    )
    if replay:
        return _idempotent_replay_response(request, replay)

    def _set_field(out, field_name, value):
        if not field_name:
            return
        out[field_name] = value

    fields = {}
    _set_field(fields, settings.PUBLISHED_PLANS_DATE_FIELD, plan_date)
    _set_field(fields, settings.PUBLISHED_PLANS_FLEET_FIELD, DatabaseService._json_dumps(payload.fleet or {}))
    _set_field(fields, settings.PUBLISHED_PLANS_ROUTES_FIELD, DatabaseService._json_dumps(normalized_routes))
    _set_field(fields, settings.PUBLISHED_PLANS_DELIVERIES_FIELD, DatabaseService._json_dumps(payload.deliveries or []))
    _set_field(fields, settings.PUBLISHED_PLANS_META_FIELD, DatabaseService._json_dumps(payload.meta or {}))
    _set_field(fields, settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD, DatabaseService._json_dumps(normalized_driver_profiles))
    _set_field(fields, settings.PUBLISHED_PLANS_UPDATED_AT_FIELD, datetime.now(ZoneInfo("UTC")).isoformat())
    _set_field(fields, settings.PUBLISHED_PLANS_STATUS_FIELD, "Published")
    _set_field(fields, settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD, user.get("email") or user.get("name") or "manager/admin")
    _set_field(fields, settings.PUBLISHED_PLANS_TYPE_FIELD, "published")

    try:
        assignment_result = svc.apply_route_assignments(plan_date, normalized_routes)

        record = {"id": str(plan_date)}
        stored = False
        storage_error = None
        try:
            record = svc.upsert_published_plan(plan_date, fields, plan_type="published")
            stored = True
        except ValueError:
            stored = False
        except requests.HTTPError as e:
            stored = False
            if e.response is not None:
                try:
                    storage_error = e.response.json()
                except ValueError:
                    storage_error = e.response.text
            else:
                storage_error = str(e)

        try:
            identities = _driver_identities_from_routes(normalized_routes)
            tokens = tokens_for_identities(identities)
            send_push_notification(
                tokens=tokens,
                title="Route Updated",
                body=(
                    f"Your route for {plan_date} has been updated."
                    if is_update
                    else f"Your route for {plan_date} has been published."
                ),
                data={
                    "kind": "route_update",
                    "plan_date": plan_date,
                    "action": "updated" if is_update else "published",
                },
            )
        except Exception:
            logger.warning("Route publish push notification failed", exc_info=True)

        response_body = {
            "ok": True,
            "plan_date": plan_date,
            "record_id": record.get("id"),
            "stored_plan_blob": stored,
            "assignments": assignment_result,
            "storage_error": storage_error,
        }
        IdempotencyService.complete(idem_context, status_code=200, body=response_body)
        resp = ok_response(request, response_body)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        IdempotencyService.abandon(idem_context)
        raise


@router.post("/plan/manual/save")
def plan_manual_save(payload: PublishPlanRequest, request: Request):
    user = _require_manager_admin(request)
    plan_date = payload.plan_date or _business_date(offset_days=0)
    force_refresh = _wants_refresh(request)

    svc = DatabaseService()
    normalized_deliveries = [_normalize_plan_delivery(d) for d in (payload.deliveries or [])]
    normalized_deliveries = _enrich_plan_delivery_coordinates(
        svc=svc,
        plan_date=plan_date,
        deliveries=normalized_deliveries,
        force_refresh=force_refresh,
    )
    normalized_deliveries = _canonical_plan_deliveries(normalized_deliveries)
    normalized_routes, normalized_driver_profiles = _normalize_route_driver_profiles(
        routes=payload.routes or [],
        meta=payload.meta or {},
        driver_profiles=payload.driver_profiles or [],
        svc=svc,
    )
    idem_context, replay = _begin_idempotent_request(
        request,
        path="/plan/manual/save",
        payload={
            "plan_date": plan_date,
            "routes": normalized_routes,
            "deliveries": normalized_deliveries,
            "meta": payload.meta or {},
            "fleet": payload.fleet or {},
            "driver_profiles": normalized_driver_profiles,
            "action": "manual_save",
        },
    )
    if replay:
        return _idempotent_replay_response(request, replay)

    normalized_routes = _normalize_manual_routes_shape(normalized_routes)
    summary = _route_metrics_summary(normalized_routes)
    missing_coordinates = _missing_delivery_coordinate_ids(normalized_deliveries)

    meta_payload = _build_manual_meta_payload(
        payload.meta if isinstance(payload.meta, dict) else {},
        plan_date=plan_date,
        deliveries=normalized_deliveries,
        routes=normalized_routes,
        fleet=payload.fleet if isinstance(payload.fleet, dict) else {},
        summary=summary if isinstance(summary, dict) else {},
    )
    if missing_coordinates:
        meta_payload["missing_delivery_coordinates"] = missing_coordinates
    else:
        meta_payload.pop("missing_delivery_coordinates", None)

    try:
        saved = svc.upsert_plan_payload(
            plan_date=plan_date,
            plan_type="manual",
            fields={
                "routes": normalized_routes,
                "deliveries": normalized_deliveries,
                "meta": meta_payload,
                "fleet": meta_payload.get("fleet") or payload.fleet or {},
                "driver_profiles": normalized_driver_profiles,
                "status": "manual",
                "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                "updated_by": user.get("email") or user.get("name") or "manager/admin",
            },
        )

        response_body = {
            "ok": True,
            "plan_date": plan_date,
            "plan_type": "manual",
            "record_id": f"{saved.get('plan_date')}::manual",
            "manual_saved": True,
            "summary": summary,
            "missing_delivery_coordinates": missing_coordinates,
            "routes": normalized_routes,
        }
        IdempotencyService.complete(idem_context, status_code=200, body=response_body)
        resp = ok_response(request, response_body)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        IdempotencyService.abandon(idem_context)
        raise


@router.get("/jobsites/list")
def jobsites_list(request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    jobsites = svc.list_jobsites_min(force_refresh=_wants_refresh(request))
    resp = ok_response(request, {"jobsites": jobsites})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/jobsites/create-json")
def jobsites_create_json(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
):
    _require_manager_admin(request)
    name_clean = (name or "").strip()
    address_clean = (address or "").strip()
    if not name_clean or not address_clean:
        raise HTTPException(status_code=400, detail="Jobsite name and address are required.")

    svc = DatabaseService()
    fields = {
        settings.JOBSITES_NAME_FIELD: name_clean,
        settings.JOBSITES_ADDRESS_FIELD: address_clean,
    }
    created = svc.create_record("Jobsites", fields)
    resp = ok_response(request, {"ok": True, "jobsite": created})
    resp.headers["Cache-Control"] = "no-store"
    return resp


class JobsiteUpdateRequest(BaseModel):
    name: str | None = None
    address: str | None = None
    site_contact: str | None = None
    site_contact_number: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@router.patch("/jobsites/{jobsite_id}")
def jobsites_update(jobsite_id: str, payload: JobsiteUpdateRequest, request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    try:
        svc.get_record("Jobsites", jobsite_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Jobsite not found.")
    fields: Dict[str, Any] = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Jobsite name cannot be empty.")
        fields[settings.JOBSITES_NAME_FIELD] = name
    if payload.address is not None:
        addr = payload.address.strip()
        if not addr:
            raise HTTPException(status_code=400, detail="Jobsite address cannot be empty.")
        fields[settings.JOBSITES_ADDRESS_FIELD] = addr
    if payload.site_contact is not None:
        contact = payload.site_contact.strip()
        fields["Site Contact"] = contact if contact else None
    if payload.site_contact_number is not None:
        contact_num = payload.site_contact_number.strip()
        fields["Site Contact Number"] = contact_num if contact_num else None
    if payload.latitude is not None:
        fields[settings.JOBSITES_LAT_FIELD] = float(payload.latitude)
    if payload.longitude is not None:
        fields[settings.JOBSITES_LON_FIELD] = float(payload.longitude)
    if not fields:
        raise HTTPException(status_code=400, detail="No updates provided.")
    updated = svc.update_record("Jobsites", jobsite_id, fields)
    resp = ok_response(request, {"ok": True, "jobsite": updated})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.delete("/jobsites/{jobsite_id}")
def jobsites_delete(jobsite_id: str, request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    try:
        deleted = svc.delete_record("Jobsites", jobsite_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    resp = ok_response(request, {"ok": True, "deleted": deleted})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/users/list")
def users_list(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    svc = DatabaseService()
    users = svc.list_users_min(active_only=False, force_refresh=_wants_refresh(request))
    resp = ok_response(request, {"users": users, "roles": ["coordinator", "driver", "manager", "admin"]})
    resp.headers["Cache-Control"] = "no-store"
    return resp


class UserCreateRequest(BaseModel):
    name: str | None = None
    email: str
    password: str
    role: str = "coordinator"
    active: bool = True


@router.post("/users/create")
def users_create(payload: UserCreateRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    svc = DatabaseService()
    existing = svc.get_user_by_email(payload.email.strip())
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered.")
    fields = {
        settings.USERS_EMAIL_FIELD: payload.email.strip(),
        settings.USERS_PASSWORD_FIELD: hash_password(payload.password),
        settings.USERS_ROLE_FIELD: payload.role.strip().lower(),
        settings.USERS_ACTIVE_FIELD: bool(payload.active),
    }
    if payload.name and payload.name.strip():
        fields[settings.USERS_NAME_FIELD] = payload.name.strip()
    created = svc.create_user(fields)
    resp = ok_response(request, {"ok": True, "record": created})
    resp.headers["Cache-Control"] = "no-store"
    return resp


class UserUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None


@router.patch("/users/{record_id}")
def users_update(record_id: str, payload: UserUpdateRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    fields = {}
    if payload.name is not None:
        fields[settings.USERS_NAME_FIELD] = payload.name.strip() if payload.name.strip() else None
    if payload.role is not None:
        fields[settings.USERS_ROLE_FIELD] = payload.role.strip().lower()
    if payload.active is not None:
        fields[settings.USERS_ACTIVE_FIELD] = bool(payload.active)
    if payload.password is not None and payload.password.strip():
        fields[settings.USERS_PASSWORD_FIELD] = hash_password(payload.password.strip())
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update.")
    svc = DatabaseService()
    try:
        updated = svc.update_record(settings.USERS_TABLE, record_id, fields)
    except ValueError:
        # Fallback: record_id might be an email with different casing/spacing.
        rec = svc.get_user_by_email(record_id)
        if not rec:
            raise HTTPException(status_code=404, detail="User not found.")
        updated = svc.update_record(settings.USERS_TABLE, rec.get("id"), fields)
    resp = ok_response(request, {"ok": True, "record": updated})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/deliveries/{delivery_id}/details")
def delivery_detail_get(delivery_id: str, request: Request):
    _require_authenticated_user(request)
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing delivery id.")
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    payload = _delivery_detail_payload(svc, rec, delivery_id)
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/deliveries/{delivery_id}")
def delivery_get(delivery_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    fields = rec.get("fields", {})
    jobsites = svc.list_jobsites_min(force_refresh=_wants_refresh(request))
    jobsite_ids = fields.get(settings.DELIVERIES_JOB_LINK_FIELD) or []
    jobsite_id = jobsite_ids[0] if isinstance(jobsite_ids, list) and jobsite_ids else None
    payload = {
        "delivery_id": fields.get("Delivery ID") or rec.get("id"),
        "record_id": rec.get("id"),
        "jobsite_id": jobsite_id,
        "sales_order": fields.get(settings.DELIVERIES_SALES_ORDER_FIELD) or "",
        "scheduled_date": fields.get("Scheduled Date") or "",
        "med_skid": fields.get("Med Skid") or fields.get("Skid") or 0,
        "small_skid": fields.get("Small Skid") or 0,
        "large_skid": fields.get("Large Skid") or 0,
        "exp_frame": fields.get("Exp Frame") or 0,
        "frame": fields.get("Frame") or 0,
        "door": fields.get("Door") or 0,
        "box": fields.get("Box") or 0,
        "pickup": bool(fields.get("Pickup")),
        "requires_empty_truck": bool(fields.get("Requires Empty Truck")),
        "window_start": fields.get("Window Start") or "",
        "window_end": fields.get("Window End") or "",
        "status": fields.get(settings.DELIVERIES_STATUS_FIELD) or "",
        "driver_notes": fields.get(settings.DELIVERIES_DRIVER_NOTES_FIELD) or "",
        "photo_urls": parse_photo_refs(fields.get(_delivery_photo_field_name())),
    }
    resp = ok_response(request, {"delivery": payload, "jobsites": jobsites})
    resp.headers["Cache-Control"] = "no-store"
    return resp


class DeliveryUpdateRequest(BaseModel):
    jobsite_id: str
    scheduled_date: str
    sales_order: str | None = None
    med_skid: int | None = 0
    small_skid: int | None = 0
    large_skid: int | None = 0
    exp_frame: int | None = 0
    frame: int | None = 0
    door: int | None = 0
    box: int | None = 0
    pickup: bool | None = False
    pickup_vehicle: str | None = None
    requires_empty_truck: bool | None = False
    window_start: str | None = None
    window_end: str | None = None

    @field_validator("scheduled_date")
    @classmethod
    def _validate_scheduled_date(cls, value):
        return parse_iso_date(value, field_name="scheduled_date", allow_none=False)


@router.patch("/deliveries/{delivery_id}")
def delivery_update(delivery_id: str, payload: DeliveryUpdateRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    fields = {
        settings.DELIVERIES_JOB_LINK_FIELD: [payload.jobsite_id],
        "Scheduled Date": payload.scheduled_date.strip(),
        "Med Skid": to_int(payload.med_skid),
        "Small Skid": to_int(payload.small_skid),
        "Large Skid": to_int(payload.large_skid),
        "Exp Frame": to_int(payload.exp_frame),
        "Frame": to_int(payload.frame),
        "Door": to_int(payload.door),
        "Box": to_int(payload.box),
        "Pickup": bool(payload.pickup),
        "Requires Empty Truck": bool(payload.requires_empty_truck),
    }
    if payload.pickup_vehicle is not None:
        pickup_vehicle = DatabaseService._normalize_pickup_vehicle(payload.pickup_vehicle)
        fields[settings.DELIVERIES_PICKUP_VEHICLE_FIELD] = pickup_vehicle or None
    if payload.sales_order is not None:
        sales_order_value = parse_sales_order(payload.sales_order)
        fields[settings.DELIVERIES_SALES_ORDER_FIELD] = sales_order_value
    if payload.window_start:
        fields["Window Start"] = payload.window_start
    else:
        fields["Window Start"] = None
    if payload.window_end:
        fields["Window End"] = payload.window_end
    else:
        fields["Window End"] = None
    updated = svc.update_record("Deliveries", rec.get("id"), fields)
    resp = ok_response(request, {"ok": True, "record": updated})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _set_solve_job(job_id: str, payload: dict[str, Any]) -> None:
    with _solve_jobs_lock:
        _solve_jobs[job_id] = payload


def _get_solve_job(job_id: str) -> dict[str, Any] | None:
    with _solve_jobs_lock:
        return _solve_jobs.get(job_id)


@router.post("/solve/run")
def solve_run(payload: SolveRunRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    job_id = uuid.uuid4().hex
    _set_solve_job(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
        },
    )

    def _runner():
        job = _get_solve_job(job_id) or {}
        job["status"] = "running"
        job["started_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
        _set_solve_job(job_id, job)
        try:
            result = generate_tomorrow_plan(
                trucks=payload.trucks,
                vans=payload.vans,
                force_refresh=payload.force_refresh,
                target_date=payload.plan_date,
            )
            job["status"] = "success"
            job["result"] = result
        except Exception as e:
            logger.exception("Solve job failed job_id=%s", job_id)
            job["status"] = "failed"
            job["error"] = str(e)
        finally:
            job["finished_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
            _set_solve_job(job_id, job)

    threading.Thread(target=_runner, daemon=True).start()

    resp = ok_response(request, {"job_id": job_id, "status": "queued"})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/solve/{job_id}")
def solve_status(job_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    job = _get_solve_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    resp = ok_response(request, job)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/debug/matrix")
def plan_debug_matrix(
    request: Request,
    date: str | None = None,
    trucks: int | None = None,
    vans: int = 0,
    route_nodes: str | None = None,
    compare_nodes: str | None = None,
):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    if trucks is not None and int(trucks) < 0:
        raise HTTPException(status_code=400, detail="trucks must be >= 0.")
    if int(vans) < 0:
        raise HTTPException(status_code=400, detail="vans must be >= 0.")

    plan_date = _require_iso_date_param(date, "date") if date else _next_plan_date()
    payload = debug_tomorrow_matrix(
        route_nodes=route_nodes,
        compare_nodes=compare_nodes,
        force_refresh=_wants_refresh(request),
        target_date=plan_date,
        trucks=trucks,
        vans=vans,
    )
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/manual")
def plan_manual(request: Request, date: str | None = None):
    _require_manager_admin(request)
    plan_date = _require_iso_date_param(date, "date") if date else _business_date(offset_days=0)
    force_refresh = _wants_refresh(request)
    svc = DatabaseService()
    payload = svc.get_plan_payload(plan_date, "manual")
    source = "manual_plans"
    manual_saved = payload is not None
    if not payload:
        payload = _build_manual_seed_payload(svc, plan_date, force_refresh=force_refresh)
        source = "deliveries"

    normalized_deliveries = [_normalize_plan_delivery(d) for d in payload.get("deliveries") or []]
    normalized_deliveries = _enrich_plan_delivery_coordinates(
        svc=svc,
        plan_date=plan_date,
        deliveries=normalized_deliveries,
        force_refresh=force_refresh,
    )
    normalized_deliveries = _canonical_plan_deliveries(normalized_deliveries)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    normalized_routes = _normalize_manual_routes_shape(payload.get("routes") or [])
    summary = _route_metrics_summary(normalized_routes)
    missing_coordinates = _missing_delivery_coordinate_ids(normalized_deliveries)
    meta = _build_manual_meta_payload(
        meta,
        plan_date=plan_date,
        deliveries=normalized_deliveries,
        routes=normalized_routes,
        fleet=payload.get("fleet") if isinstance(payload.get("fleet"), dict) else {},
        summary=summary if isinstance(summary, dict) else {},
    )
    if missing_coordinates:
        meta["missing_delivery_coordinates"] = missing_coordinates
    else:
        meta.pop("missing_delivery_coordinates", None)
    payload["meta"] = meta

    resp = ok_response(
        request,
        {
            "plan_date": plan_date,
            "plan_type": "manual",
            "published": bool(manual_saved),
            "manual_saved": bool(manual_saved),
            "record_id": f"{plan_date}::manual",
            "routes": normalized_routes,
            "deliveries": normalized_deliveries,
            "meta": meta,
            "fleet": payload.get("fleet") or meta.get("fleet") or {},
            "driver_profiles": payload.get("driver_profiles") or [],
            "published_by": payload.get("updated_by"),
            "status": payload.get("status"),
            "updated_at": payload.get("updated_at"),
            "created_at": payload.get("updated_at"),
            "source": source,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/manual/list")
def plan_manual_list(request: Request):
    _require_manager_admin(request)
    svc = DatabaseService()
    records = svc.list_published_plans(force_refresh=_wants_refresh(request), plan_type="manual")
    items: list[dict[str, Any]] = []
    for rec in records:
        fields = rec.get("fields", {}) if isinstance(rec, dict) else {}
        meta_raw = fields.get(settings.PUBLISHED_PLANS_META_FIELD)
        meta = svc._json_load(meta_raw, {}) if meta_raw not in (None, "") else {}
        summary = (
            meta.get("manual_metrics")
            if isinstance(meta, dict) and isinstance(meta.get("manual_metrics"), dict)
            else {}
        )
        if not summary:
            routes_raw = fields.get(settings.PUBLISHED_PLANS_ROUTES_FIELD)
            routes = svc._json_load(routes_raw, []) if routes_raw not in (None, "") else []
            summary = _route_metrics_summary(routes if isinstance(routes, list) else [])
        items.append(
            {
                "record_id": rec.get("id"),
                "plan_date": fields.get(settings.PUBLISHED_PLANS_DATE_FIELD),
                "plan_type": fields.get(settings.PUBLISHED_PLANS_TYPE_FIELD) or "manual",
                "published_by": fields.get(settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD),
                "status": fields.get(settings.PUBLISHED_PLANS_STATUS_FIELD),
                "updated_at": fields.get(settings.PUBLISHED_PLANS_UPDATED_AT_FIELD),
                "created_at": rec.get("createdTime"),
                "summary": summary,
            }
        )
    resp = ok_response(request, {"items": items})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/published")
def plan_published(request: Request, date: str | None = None):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    plan_date = _require_iso_date_param(date, "date") if date else _business_date(offset_days=0)
    svc = DatabaseService()
    payload = svc.get_plan_payload(plan_date, "published")
    source = "published_plans"
    if not payload:
        payload = svc.build_delivery_plan_payload(plan_date, force_refresh=_wants_refresh(request))
        source = "deliveries"
        if not payload.get("deliveries"):
            return ok_response(request, {"plan_date": plan_date, "plan_type": "published", "published": False})
    if not payload:
        return ok_response(request, {"plan_date": plan_date, "plan_type": "published", "published": False})
    normalized_deliveries = [_normalize_plan_delivery(d) for d in payload.get("deliveries") or []]

    meta = payload.get("meta") or {}
    if isinstance(meta, dict):
        if meta.get("depot_lat") is None:
            meta["depot_lat"] = settings.DEPOT_LAT
        if meta.get("depot_lon") is None:
            meta["depot_lon"] = settings.DEPOT_LON

    resp = ok_response(
        request,
        {
            "plan_date": plan_date,
            "plan_type": "published",
            "published": True,
            "record_id": str(payload.get("plan_date") or plan_date),
            "routes": payload.get("routes") or [],
            "deliveries": normalized_deliveries,
            "meta": meta,
            "fleet": payload.get("fleet") or {},
            "driver_profiles": payload.get("driver_profiles") or [],
            "published_by": payload.get("updated_by"),
                "status": payload.get("status") or "manual",
                "updated_at": payload.get("updated_at"),
                "created_at": payload.get("updated_at"),
                "source": source,
            },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/published/list")
def plan_published_list(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    records = svc.list_published_plans(force_refresh=_wants_refresh(request), plan_type="published")
    items = []
    for rec in records:
        fields = rec.get("fields", {})
        items.append(
            {
                "record_id": rec.get("id"),
                "plan_date": fields.get(settings.PUBLISHED_PLANS_DATE_FIELD),
                "plan_type": fields.get(settings.PUBLISHED_PLANS_TYPE_FIELD) or "published",
                "published_by": fields.get(settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD),
                "status": fields.get(settings.PUBLISHED_PLANS_STATUS_FIELD),
                "updated_at": fields.get(settings.PUBLISHED_PLANS_UPDATED_AT_FIELD),
                "created_at": rec.get("createdTime"),
            }
        )
    if not items:
        seen_dates: set[str] = set()
        for delivery in svc.list_deliveries_min(force_refresh=_wants_refresh(request)):
            scheduled = str(delivery.get("scheduled_date") or "").strip()
            if not scheduled:
                continue
            seen_dates.add(scheduled)
        for scheduled in sorted(seen_dates, reverse=True):
            items.append(
                {
                    "record_id": scheduled,
                    "plan_date": scheduled,
                    "plan_type": "published",
                    "published_by": None,
                    "status": "Live",
                    "updated_at": None,
                    "created_at": None,
                    "source": "deliveries",
                }
            )
    resp = ok_response(request, {"items": items})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/dashboard")
def plan_dashboard(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    vehicle_type: str = "all",
):
    _require_manager_admin(request)

    normalized_vehicle_type = _text_or_empty(vehicle_type).lower() or "all"
    if normalized_vehicle_type not in _DASHBOARD_VEHICLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"vehicle_type must be one of: {', '.join(sorted(_DASHBOARD_VEHICLE_TYPES))}.",
        )

    selected_end = _require_iso_date_param(end_date, "end_date") if end_date else _business_date(offset_days=0)
    if start_date:
        selected_start = _require_iso_date_param(start_date, "start_date")
    else:
        selected_start = (date.fromisoformat(selected_end) - timedelta(days=29)).isoformat()

    start_dt = date.fromisoformat(selected_start)
    end_dt = date.fromisoformat(selected_end)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date.")

    range_days = (end_dt - start_dt).days + 1
    max_range_days = int(settings.DASHBOARD_MAX_RANGE_DAYS)
    if range_days > max_range_days:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large ({range_days} days). Max supported range is {max_range_days} days.",
        )

    svc = DatabaseService()
    manual_meta = svc.list_plan_meta("manual")
    published_meta = svc.list_plan_meta("published")

    def _date_in_range(value: str) -> bool:
        return selected_start <= value <= selected_end

    manual_dates = {
        str(item.get("plan_date") or "").strip()
        for item in manual_meta or []
        if str(item.get("plan_date") or "").strip() and _date_in_range(str(item.get("plan_date") or "").strip())
    }
    published_dates = {
        str(item.get("plan_date") or "").strip()
        for item in published_meta or []
        if str(item.get("plan_date") or "").strip() and _date_in_range(str(item.get("plan_date") or "").strip())
    }

    compared_dates = sorted(manual_dates & published_dates)
    manual_only_dates = sorted(manual_dates - published_dates)
    published_only_dates = sorted(published_dates - manual_dates)

    totals_manual = _empty_dashboard_metrics()
    totals_published = _empty_dashboard_metrics()
    timeline: list[dict[str, Any]] = []
    effective_compared_dates: list[str] = []

    for plan_date in compared_dates:
        manual_payload = svc.get_plan_payload(plan_date, "manual")
        published_payload = svc.get_plan_payload(plan_date, "published")
        if not manual_payload or not published_payload:
            continue

        manual_routes = _normalize_manual_routes_shape(manual_payload.get("routes") or [])
        published_routes_raw = published_payload.get("routes")
        published_routes = published_routes_raw if isinstance(published_routes_raw, list) else []

        manual_metrics = _plan_dashboard_metrics(manual_routes, vehicle_type=normalized_vehicle_type)
        published_metrics = _plan_dashboard_metrics(published_routes, vehicle_type=normalized_vehicle_type)
        delta_metrics = _delta_dashboard_metrics(manual_metrics, published_metrics)
        day_improvements = _dashboard_improvements(manual_metrics, published_metrics, 1)

        totals_manual = _sum_dashboard_metrics(totals_manual, manual_metrics)
        totals_published = _sum_dashboard_metrics(totals_published, published_metrics)
        effective_compared_dates.append(plan_date)

        timeline.append(
            {
                "date": plan_date,
                "manual": manual_metrics,
                "published": published_metrics,
                "delta_published_minus_manual": delta_metrics,
                "improvements": day_improvements,
            }
        )

    totals_delta = _delta_dashboard_metrics(totals_manual, totals_published)
    totals_improvements = _dashboard_improvements(
        totals_manual,
        totals_published,
        len(effective_compared_dates),
    )

    covered_dates = manual_dates | published_dates
    coverage = {
        "range_days": int(range_days),
        "manual_dates": len(manual_dates),
        "published_dates": len(published_dates),
        "compared_dates": len(effective_compared_dates),
        "manual_only_dates": len(manual_only_dates),
        "published_only_dates": len(published_only_dates),
        "empty_dates": max(0, int(range_days) - len(covered_dates)),
        "compared_date_list": effective_compared_dates,
        "manual_only_date_list": manual_only_dates,
        "published_only_date_list": published_only_dates,
    }

    payload = {
        "start_date": selected_start,
        "end_date": selected_end,
        "vehicle_type": normalized_vehicle_type,
        "coverage": coverage,
        "totals": {
            "manual": totals_manual,
            "published": totals_published,
            "delta_published_minus_manual": totals_delta,
            "improvements": totals_improvements,
        },
        "timeline": timeline,
        "highlights": _dashboard_highlights(totals_improvements, len(effective_compared_dates)),
    }
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/custom-stop/estimate")
def plan_custom_stop_estimate(payload: CustomStopEstimateRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    if payload.prev.lat is None or payload.prev.lon is None:
        raise HTTPException(status_code=400, detail="Missing prev coordinates.")
    if payload.next.lat is None or payload.next.lon is None:
        raise HTTPException(status_code=400, detail="Missing next coordinates.")

    custom_lat = payload.custom.lat
    custom_lon = payload.custom.lon
    if custom_lat is None or custom_lon is None:
        if payload.custom.address:
            mb = MapboxService()
            lat, lon = mb.get_coordinates(payload.custom.address)
            custom_lat, custom_lon = lat, lon
        if custom_lat is None or custom_lon is None:
            raise HTTPException(status_code=400, detail="Missing custom coordinates/address.")

    coords_list = [
        (float(payload.prev.lon), float(payload.prev.lat)),
        (float(custom_lon), float(custom_lat)),
        (float(payload.next.lon), float(payload.next.lat)),
    ]

    matrix = MapboxService.get_matrix(coords_list)
    durations = matrix.get("durations") or []
    distances = matrix.get("distances") or []
    try:
        t_prev_custom = float(durations[0][1])
        t_custom_next = float(durations[1][2])
        t_prev_next = float(durations[0][2])
        d_prev_custom = float(distances[0][1])
        d_custom_next = float(distances[1][2])
        d_prev_next = float(distances[0][2])
    except Exception:
        raise HTTPException(status_code=400, detail="Mapbox matrix returned invalid data.")

    resp = ok_response(
        request,
        {
            "ok": True,
            "custom_lat": float(custom_lat),
            "custom_lon": float(custom_lon),
            "t_prev_custom": t_prev_custom,
            "t_custom_next": t_custom_next,
            "t_prev_next": t_prev_next,
            "d_prev_custom": d_prev_custom,
            "d_custom_next": d_custom_next,
            "d_prev_next": d_prev_next,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/deliveries/{delivery_id}/reschedule")
def delivery_reschedule(delivery_id: str, payload: RescheduleRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    scheduled_date = payload.scheduled_date.strip() if payload.scheduled_date else None
    fields = {"Scheduled Date": scheduled_date} if scheduled_date else {"Scheduled Date": None}
    updated = svc.update_record("Deliveries", rec.get("id"), fields)

    resp = ok_response(
        request,
        {
            "ok": True,
            "record_id": updated.get("id"),
            "scheduled_date": scheduled_date,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/deliveries/{delivery_id}/ps-delivered")
def delivery_ps_delivered(delivery_id: str, payload: PSDeliveredRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    delivered = bool(payload.delivered)
    fields = {settings.DELIVERIES_PS_DELIVERED_FIELD: delivered}
    updated = svc.update_record("Deliveries", rec.get("id"), fields)

    resp = ok_response(
        request,
        {
            "ok": True,
            "record_id": updated.get("id"),
            "delivery_id": delivery_id,
            "ps_delivered": delivered,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/deliveries/bulk-create")
def deliveries_bulk_create(payload: BulkCreateDeliveriesRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    idem_context, replay = _begin_idempotent_request(
        request,
        path="/deliveries/bulk-create",
        payload={
            "deliveries": [item.model_dump(exclude_none=True) for item in (payload.deliveries or [])],
            "action": "bulk_create_deliveries",
        },
    )
    if replay:
        return _idempotent_replay_response(request, replay)

    svc = DatabaseService()
    try:
        known_jobsites: dict[str, bool] = {}
        results = []
        for idx, item in enumerate(payload.deliveries or []):
            errors = []
            jobsite_id = (item.jobsite_id or "").strip()
            scheduled_date = (item.scheduled_date or "").strip()
            sales_order = (item.sales_order or "").strip()

            if not jobsite_id:
                errors.append("Missing jobsite_id.")
            if not scheduled_date:
                errors.append("Missing scheduled_date.")
            if not sales_order:
                errors.append("Missing sales_order.")

            if jobsite_id:
                exists = known_jobsites.get(jobsite_id)
                if exists is None:
                    try:
                        svc.get_record("Jobsites", jobsite_id)
                        exists = True
                    except Exception:
                        exists = False
                    known_jobsites[jobsite_id] = exists
                if not exists:
                    errors.append("Jobsite not found. Select a jobsite that already exists.")

            if errors:
                results.append({"ok": False, "index": idx, "error": " ".join(errors)})
                continue

            pickup_vehicle = DatabaseService._normalize_pickup_vehicle(item.pickup_vehicle)
            if bool(item.pickup) and not pickup_vehicle:
                pickup_vehicle = "both"
            if not bool(item.pickup):
                pickup_vehicle = ""

            fields = {
                settings.DELIVERIES_JOB_LINK_FIELD: [jobsite_id],
                "Scheduled Date": scheduled_date,
                "Med Skid": int(item.med_skid or 0),
                "Large Skid": int(item.large_skid or 0),
                "Small Skid": int(item.small_skid or 0),
                "Frame": int(item.frame or 0),
                "Exp Frame": int(item.exp_frame or 0),
                "Door": int(item.door or 0),
                "Box": int(item.box or 0),
                "Pickup": bool(item.pickup),
                settings.DELIVERIES_PICKUP_VEHICLE_FIELD: pickup_vehicle or None,
                "Requires Empty Truck": bool(item.requires_empty_truck),
                "Window Start": (item.window_start or "").strip() or None,
                "Window End": (item.window_end or "").strip() or None,
            }
            sales_order_value = parse_sales_order(sales_order)
            if sales_order_value is not None:
                fields[settings.DELIVERIES_SALES_ORDER_FIELD] = sales_order_value

            try:
                created = svc.create_record("Deliveries", fields)
            except requests.HTTPError as e:
                detail = None
                if e.response is not None:
                    try:
                        detail = e.response.json()
                    except Exception:
                        detail = e.response.text
                results.append(
                    {
                        "ok": False,
                        "index": idx,
                        "error": "Airtable error while creating delivery.",
                        "detail": detail or str(e),
                    }
                )
                continue
            results.append({"ok": True, "index": idx, "record_id": created.get("id")})

        ok_count = sum(1 for r in results if r.get("ok"))
        response_body = {"ok": ok_count == len(results), "count": ok_count, "results": results}
        IdempotencyService.complete(idem_context, status_code=200, body=response_body)
        resp = ok_response(request, response_body)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        IdempotencyService.abandon(idem_context)
        raise


@router.post("/deliveries/bulk-update")
def deliveries_bulk_update(payload: BulkUpdateDeliveriesRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    items = payload.deliveries or []
    if not items:
        raise HTTPException(status_code=400, detail="No deliveries provided.")

    idem_context, replay = _begin_idempotent_request(
        request,
        path="/deliveries/bulk-update",
        payload={
            "deliveries": [item.model_dump(exclude_none=True) for item in items],
            "action": "bulk_update_deliveries",
        },
    )
    if replay:
        return _idempotent_replay_response(request, replay)

    svc = DatabaseService()
    try:
        result = svc.bulk_update_deliveries_by_business_id(
            [item.model_dump(exclude_none=True) for item in items]
        )
        errors = result.get("errors") or []
        response_body = {"ok": len(errors) == 0, **result}
        IdempotencyService.complete(idem_context, status_code=200, body=response_body)
        resp = ok_response(request, response_body)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        IdempotencyService.abandon(idem_context)
        raise


@router.delete("/deliveries/{delivery_id}")
def deliveries_delete(delivery_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    deleted = svc.delete_record("Deliveries", delivery_id)
    resp = ok_response(request, {"ok": True, "deleted": deleted})
    resp.headers["Cache-Control"] = "no-store"
    return resp
