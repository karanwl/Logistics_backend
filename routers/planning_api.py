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
from planning import generate_tomorrow_plan, recalculate_tomorrow_plan
from push_notifications import send_push_notification, tokens_for_identities
from services import DatabaseService, MapboxService
from config import settings
from security import csrf_protect

from datetime import datetime
from zoneinfo import ZoneInfo
from responses import ok_response
from utils import business_date_iso, next_plan_date_iso, parse_sales_order, parse_iso_date, to_int

router = APIRouter(dependencies=[Depends(csrf_protect)])
logger = logging.getLogger(__name__)

_solve_jobs_lock = threading.Lock()
_solve_jobs: dict[str, dict[str, Any]] = {}


def _wants_refresh(request: Request) -> bool:
    refresh = (request.query_params.get("refresh") or "").strip().lower()
    if refresh in {"1", "true", "yes", "y"}:
        return True
    cache_control = (request.headers.get("cache-control") or "").lower()
    pragma = (request.headers.get("pragma") or "").lower()
    return "no-cache" in cache_control or "max-age=0" in cache_control or "no-cache" in pragma


def _require_manager_admin(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    return user


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


class BulkUpdateDeliveriesRequest(BaseModel):
    deliveries: list[BulkUpdateDeliveryItem] = Field(default_factory=list)


@router.get("/drivers/list")
def drivers_list(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    records = svc.list_users_min(role=settings.DRIVERS_ROLE_VALUE, active_only=False, force_refresh=_wants_refresh(request))
    drivers = [d.get("display_name") or d.get("name") for d in records if d.get("display_name") or d.get("name")]
    resp = ok_response(request, {"drivers": drivers})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/drivers/list-full")
def drivers_list_full(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
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
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    live_date = _require_iso_date_param(date, "date") if date else _business_date(offset_days=0)
    svc = DatabaseService()
    payload = svc.get_live_progress_snapshot(live_date=live_date, force_refresh=_wants_refresh(request))
    resp = ok_response(request, payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/tomorrow")
def plan_tomorrow(fleet: FleetRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    force_refresh = _wants_refresh(request)
    resp = ok_response(
        request,
        generate_tomorrow_plan(trucks=fleet.trucks, vans=fleet.vans, force_refresh=force_refresh),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/recalculate")
def plan_recalculate(payload: list[RecalculateRoute], request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    routes = [r.model_dump() for r in payload]
    force_refresh = _wants_refresh(request)
    resp = ok_response(request, recalculate_tomorrow_plan(routes, force_refresh=force_refresh))
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

    resp = ok_response(
        request,
        {
            "ok": True,
            "plan_date": plan_date,
            "record_id": record.get("id"),
            "stored_plan_blob": stored,
            "assignments": assignment_result,
            "storage_error": storage_error,
        },
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
            "status": payload.get("status"),
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

    svc = DatabaseService()
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
    resp = ok_response(request, {"ok": ok_count == len(results), "count": ok_count, "results": results})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/deliveries/bulk-update")
def deliveries_bulk_update(payload: BulkUpdateDeliveriesRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    items = payload.deliveries or []
    if not items:
        raise HTTPException(status_code=400, detail="No deliveries provided.")

    svc = DatabaseService()
    result = svc.bulk_update_deliveries_by_business_id(
        [item.model_dump(exclude_none=True) for item in items]
    )
    resp = ok_response(request, {"ok": True, **result})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
