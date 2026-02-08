from fastapi import APIRouter, Depends
import html
import json
import logging

from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from fastapi import HTTPException

import requests

from typing import Any, Dict
import uuid
import threading

from auth import get_current_user, hash_password
from planning import debug_tomorrow_matrix, generate_tomorrow_plan, recalculate_tomorrow_plan
from services import DatabaseService, MapboxService
from config import settings
from security import csrf_protect

from datetime import datetime
from zoneinfo import ZoneInfo
from utils import business_date_iso, next_plan_date_iso, parse_sales_order, to_int

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


class FleetRequest(BaseModel):
    trucks: int = Field(default=settings.PHYSICAL_TRUCKS, ge=0)
    vans: int = Field(default=0, ge=0)


class SolveRunRequest(BaseModel):
    trucks: int = Field(default=settings.PHYSICAL_TRUCKS, ge=0)
    vans: int = Field(default=0, ge=0)
    force_refresh: bool = False
    plan_date: str | None = None


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
    requires_empty_truck: bool = False


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
    resp = JSONResponse({"drivers": drivers})
    resp.headers["Cache-Control"] = "no-store"
    return resp



@router.get("/deliveries/all")
def deliveries_all(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Login required.")
    svc = DatabaseService()
    deliveries = svc.list_deliveries_min(force_refresh=_wants_refresh(request))
    resp = JSONResponse({"deliveries": deliveries, "count": len(deliveries)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/tomorrow")
def plan_tomorrow(fleet: FleetRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    force_refresh = _wants_refresh(request)
    resp = JSONResponse(generate_tomorrow_plan(trucks=fleet.trucks, vans=fleet.vans, force_refresh=force_refresh))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/plan/recalculate")
def plan_recalculate(payload: list[RecalculateRoute], request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    routes = [r.model_dump() for r in payload]
    force_refresh = _wants_refresh(request)
    resp = JSONResponse(recalculate_tomorrow_plan(routes, force_refresh=force_refresh))
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

    if "requires_truck" not in d:
        d["requires_truck"] = any(
            d.get(field, 0) > 0 for field in ("med_skid", "large_skid", "small_skid", "door", "frame", "exp_frame")
        )
    return d


@router.post("/plan/publish")
def plan_publish(payload: PublishPlanRequest, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")

    plan_date = payload.plan_date or _next_plan_date()

    svc = DatabaseService()
    def _set_field(out, field_name, value):
        if not field_name:
            return
        out[field_name] = value

    fields = {}
    _set_field(fields, settings.PUBLISHED_PLANS_DATE_FIELD, plan_date)
    _set_field(fields, settings.PUBLISHED_PLANS_FLEET_FIELD, DatabaseService._json_dumps(payload.fleet or {}))
    _set_field(fields, settings.PUBLISHED_PLANS_ROUTES_FIELD, DatabaseService._json_dumps(payload.routes or []))
    _set_field(fields, settings.PUBLISHED_PLANS_DELIVERIES_FIELD, DatabaseService._json_dumps(payload.deliveries or []))
    _set_field(fields, settings.PUBLISHED_PLANS_META_FIELD, DatabaseService._json_dumps(payload.meta or {}))
    _set_field(fields, settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD, DatabaseService._json_dumps(payload.driver_profiles or []))
    _set_field(fields, settings.PUBLISHED_PLANS_UPDATED_AT_FIELD, datetime.now(ZoneInfo("UTC")).isoformat())
    _set_field(fields, settings.PUBLISHED_PLANS_STATUS_FIELD, "Published")
    _set_field(fields, settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD, user.get("email") or user.get("name") or "manager/admin")
    _set_field(fields, settings.PUBLISHED_PLANS_TYPE_FIELD, "published")

    try:
        record = svc.upsert_published_plan(plan_date, fields, plan_type="published")
    except requests.HTTPError as e:
        detail = None
        if e.response is not None:
            try:
                detail = e.response.json()
            except ValueError:
                detail = e.response.text
        resp = JSONResponse(
            {
                "ok": False,
                "error": "Airtable error while publishing plan.",
                "detail": detail or str(e),
                "fields": list(fields.keys()),
                "table": settings.PUBLISHED_PLANS_TABLE,
            },
            status_code=400,
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp
    resp = JSONResponse(
        {
            "ok": True,
            "plan_date": plan_date,
            "record_id": record.get("id"),
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/jobsites/list")
def jobsites_list(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    jobsites = svc.list_jobsites_min(force_refresh=_wants_refresh(request))
    resp = JSONResponse({"jobsites": jobsites})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/users/list")
def users_list(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    svc = DatabaseService()
    users = svc.list_users_min(active_only=False, force_refresh=_wants_refresh(request))
    resp = JSONResponse({"users": users, "roles": ["coordinator", "driver", "manager", "admin"]})
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
    resp = JSONResponse({"ok": True, "record": created})
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
    updated = svc.update_record(settings.USERS_TABLE, record_id, fields)
    resp = JSONResponse({"ok": True, "record": updated})
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
    resp = JSONResponse({"delivery": payload, "jobsites": jobsites})
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
    requires_empty_truck: bool | None = False
    window_start: str | None = None
    window_end: str | None = None


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
    resp = JSONResponse({"ok": True, "record": updated})
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

    resp = JSONResponse({"job_id": job_id, "status": "queued"})
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
    resp = JSONResponse(job)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/matrix-debug")
def plan_matrix_debug(
    request: Request,
    route_nodes: str | None = None,
    compare_nodes: str | None = None,
    format: str | None = None,
):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    force_refresh = _wants_refresh(request)
    data = debug_tomorrow_matrix(
        route_nodes=route_nodes,
        compare_nodes=compare_nodes,
        force_refresh=force_refresh,
    )
    wants_html = str(format or "").strip().lower() != "json"
    if wants_html:
        def _render_leg_table(legs: list[dict], title: str) -> str:
            if not legs:
                return f"<h3>{html.escape(title)}</h3><p>No legs.</p>"
            rows = []
            for leg in legs:
                rows.append(
                    "<tr>"
                    f"<td>{leg.get('from_node')}</td>"
                    f"<td>{html.escape(str(leg.get('from_label') or ''))}</td>"
                    f"<td>{leg.get('to_node')}</td>"
                    f"<td>{html.escape(str(leg.get('to_label') or ''))}</td>"
                    f"<td>{leg.get('distance_m')}</td>"
                    f"<td>{leg.get('duration_s')}</td>"
                    "</tr>"
                )
            return (
                f"<h3>{html.escape(title)}</h3>"
                "<div class='table-wrap'><table><thead><tr>"
                "<th>From Node</th><th>From</th><th>To Node</th><th>To</th><th>Dist (m)</th><th>Dur (s)</th>"
                f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            )

        def _render_matrix_table(
            matrix_rows: list[list[int | None]],
            title: str,
            flagged_cells: set[tuple[int, int]] | None = None,
            flagged_class: str = "cell-flag",
        ) -> str:
            if not matrix_rows:
                return f"<h3>{html.escape(title)}</h3><p>No matrix data.</p>"
            n = len(matrix_rows)
            head_cells = "".join(f"<th>{i}</th>" for i in range(n))
            body_rows = []
            for i, row in enumerate(matrix_rows):
                cells = []
                for j, v in enumerate(row):
                    cls = f" class='{flagged_class}'" if flagged_cells and (i, j) in flagged_cells else ""
                    cells.append(f"<td{cls}>{'' if v is None else v}</td>")
                body_rows.append(f"<tr><th>{i}</th>{''.join(cells)}</tr>")
            return (
                f"<h3>{html.escape(title)}</h3>"
                "<div class='table-wrap matrix-wrap'><table><thead><tr><th></th>"
                f"{head_cells}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
            )

        coord_rows = "".join(
            "<tr>"
            f"<td>{c.get('node')}</td>"
            f"<td>{c.get('lon')}</td>"
            f"<td>{c.get('lat')}</td>"
            "</tr>"
            for c in (data.get("coordinates") or [])
        )
        delivery_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(d.get('delivery_id') or ''))}</td>"
            f"<td>{html.escape(str(d.get('job_name') or ''))}</td>"
            f"<td>{html.escape(str(d.get('job_address') or ''))}</td>"
            f"<td>{d.get('lon')}</td>"
            f"<td>{d.get('lat')}</td>"
            "</tr>"
            for d in (data.get("deliveries") or [])
        )
        node_rows = []
        for n in (data.get("nodes") or []):
            token_lines = []
            for t in (n.get("tokens") or []):
                token_lines.append(
                    f"{html.escape(str(t.get('token') or ''))} -> "
                    f"{html.escape(str(t.get('job_name') or t.get('delivery_id') or ''))}"
                )
            node_rows.append(
                "<tr>"
                f"<td>{n.get('node')}</td>"
                f"<td>{html.escape(str(n.get('label') or ''))}</td>"
                f"<td>{n.get('lon')}</td>"
                f"<td>{n.get('lat')}</td>"
                f"<td>{'<br>'.join(token_lines)}</td>"
                "</tr>"
            )
        raw_json = html.escape(json.dumps(data.get("mapbox_raw"), indent=2))
        correction_meta = data.get("distance_correction_meta") or {}
        flagged_cells = {
            (int(f.get("from_node")), int(f.get("to_node")))
            for f in (data.get("raw_haversine_flags") or [])
            if f.get("from_node") is not None and f.get("to_node") is not None
        }
        corrected_cells = {
            (int(c.get("from_node")), int(c.get("to_node")))
            for c in (data.get("distance_corrections") or [])
            if c.get("replaced") and c.get("from_node") is not None and c.get("to_node") is not None
        }
        correction_rows = "".join(
            "<tr>"
            f"<td>{c.get('from_node')}</td>"
            f"<td>{c.get('to_node')}</td>"
            f"<td>{c.get('matrix_m')}</td>"
            f"<td>{c.get('haversine_m')}</td>"
            f"<td>{round((float(c.get('matrix_m') or 0) / float(c.get('haversine_m') or 1)), 3) if (c.get('haversine_m') or 0) else '--'}</td>"
            f"<td>{c.get('directions_m') if c.get('replaced') else '--'}</td>"
            f"<td>{round((float(c.get('directions_m') or 0) / float(c.get('haversine_m') or 1)), 3) if (c.get('replaced') and (c.get('haversine_m') or 0)) else '--'}</td>"
            f"<td>{'yes' if c.get('replaced') else 'no'}</td>"
            f"<td>{html.escape(str(c.get('source') or c.get('error') or ''))}</td>"
            "</tr>"
            for c in (data.get("distance_corrections") or [])
        )
        original_matrix = data.get("original_distance_matrix_m") or []
        haversine_matrix = data.get("haversine_matrix_m") or []
        factor_values = []
        n_rows = min(len(original_matrix), len(haversine_matrix))
        for i in range(n_rows):
            row_o = original_matrix[i] if isinstance(original_matrix[i], list) else []
            row_h = haversine_matrix[i] if isinstance(haversine_matrix[i], list) else []
            width = min(len(row_o), len(row_h))
            for j in range(width):
                if i == j:
                    continue
                ov = row_o[j]
                hv = row_h[j]
                if ov in (None, "") or hv in (None, ""):
                    continue
                try:
                    ovf = float(ov)
                    hvf = float(hv)
                except Exception:
                    continue
                if ovf <= 0 or hvf <= 0:
                    continue
                factor_values.append(
                    {
                        "from_node": i,
                        "to_node": j,
                        "original_m": int(ovf),
                        "haversine_m": int(hvf),
                        "factor": ovf / hvf,
                        "flagged": (i, j) in flagged_cells,
                        "corrected": (i, j) in corrected_cells,
                    }
                )
        factor_values.sort(key=lambda x: x["factor"], reverse=True)
        factor_rows = "".join(
            "<tr>"
            f"<td>{f['from_node']}</td>"
            f"<td>{f['to_node']}</td>"
            f"<td>{f['original_m']}</td>"
            f"<td>{f['haversine_m']}</td>"
            f"<td>{round(float(f['factor']), 3)}</td>"
            f"<td>{'yes' if f['flagged'] else 'no'}</td>"
            f"<td>{'yes' if f['corrected'] else 'no'}</td>"
            "</tr>"
            for f in factor_values
        )
        primary = data.get("primary_route") or {}
        compare = data.get("compare_route") or {}
        html_payload = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Matrix Debug</title>"
            "<style>"
            "body{font-family:Manrope,Segoe UI,Arial,sans-serif;margin:16px;background:#f7f8fb;color:#0f172a}"
            "h1,h2,h3{margin:10px 0} .meta{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px}"
            ".chip{background:#e2e8f0;padding:6px 10px;border-radius:999px;font-size:12px}"
            ".table-wrap{overflow:auto;border:1px solid #cbd5e1;border-radius:10px;background:#fff}"
            "table{border-collapse:collapse;width:100%;font-size:12px} th,td{border:1px solid #e2e8f0;padding:6px;vertical-align:top}"
            "th{background:#f1f5f9;position:sticky;top:0;z-index:1} .matrix-wrap{max-height:420px}"
            ".cell-flag{background:#fee2e2;color:#991b1b;font-weight:700}"
            ".cell-corrected{background:#dcfce7;color:#14532d;font-weight:700}"
            "details{margin-top:12px;background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:10px}"
            "pre{white-space:pre-wrap;font-size:11px;line-height:1.3}"
            "</style></head><body>"
            "<h1>Mapbox Matrix Debug</h1>"
            f"<div class='meta'><span class='chip'>Plan Date: {html.escape(str(data.get('target_date') or ''))}</span>"
            f"<span class='chip'>Nodes: {data.get('node_count')}</span>"
            f"<span class='chip'>Deliveries: {data.get('delivery_count')}</span>"
            f"<span class='chip'>Factor: {correction_meta.get('factor', '--')}</span>"
            f"<span class='chip'>Corrected Pairs: {correction_meta.get('replaced_pairs', 0)}/{correction_meta.get('checked_pairs', 0)}</span>"
            f"<span class='chip'>Directions Checked: {data.get('directions_checked_pairs', 0)}</span>"
            f"<span class='chip'>Raw Haversine > {correction_meta.get('raw_haversine_factor_threshold', 1.5)}: {correction_meta.get('raw_haversine_flagged_pairs', 0)}</span>"
            f"<span class='chip'>Route Nodes: {html.escape(route_nodes or '')}</span>"
            f"<span class='chip'>Compare Nodes: {html.escape(compare_nodes or '')}</span></div>"
            "<h2>Coordinates (Node Index)</h2>"
            "<div class='table-wrap'><table><thead><tr><th>Node</th><th>Lon</th><th>Lat</th></tr></thead>"
            f"<tbody>{coord_rows}</tbody></table></div>"
            "<h2>Deliveries (Job + Coordinates)</h2>"
            "<div class='table-wrap'><table><thead><tr><th>Delivery ID</th><th>Job Name</th><th>Address</th><th>Lon</th><th>Lat</th></tr></thead>"
            f"<tbody>{delivery_rows}</tbody></table></div>"
            "<h2>Node Mapping (Node -> Tokens -> Jobs)</h2>"
            "<div class='table-wrap'><table><thead><tr><th>Node</th><th>Label</th><th>Lon</th><th>Lat</th><th>Tokens</th></tr></thead>"
            f"<tbody>{''.join(node_rows)}</tbody></table></div>"
            f"{_render_leg_table(primary.get('legs') or [], 'Primary Route Legs')}"
            f"<p><strong>Primary Total:</strong> {primary.get('total_distance_m','--')} m, {primary.get('total_duration_s','--')} s</p>"
            f"{_render_leg_table(compare.get('legs') or [], 'Compare Route Legs')}"
            f"<p><strong>Compare Total:</strong> {compare.get('total_distance_m','--')} m, {compare.get('total_duration_s','--')} s</p>"
            "<h2>Distance Corrections (Matrix vs Haversine vs Directions)</h2>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>From Node</th><th>To Node</th><th>Matrix (m)</th><th>Haversine (m)</th><th>Orig Factor</th><th>Directions (m)</th><th>Corrected Factor</th><th>Replaced</th><th>Source/Error</th>"
            f"</tr></thead><tbody>{correction_rows}</tbody></table></div>"
            f"{_render_matrix_table(data.get('original_distance_matrix_m') or [], 'Original Matrix Distance (meters)', flagged_cells=flagged_cells)}"
            f"{_render_matrix_table(data.get('haversine_matrix_m') or [], 'Haversine Matrix (meters)')}"
            f"{_render_matrix_table(data.get('corrected_distance_matrix_m') or data.get('distance_matrix_m') or [], 'Corrected Matrix Distance (meters)', flagged_cells=corrected_cells, flagged_class='cell-corrected')}"
            f"{_render_matrix_table(data.get('directions_api_matrix_m') or [], 'Directions API Matrix (meters, - = not checked)')}"
            "<h2>Factor Values Table (Original / Haversine)</h2>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>From Node</th><th>To Node</th><th>Original (m)</th><th>Haversine (m)</th><th>Factor</th><th>Flagged</th><th>Corrected</th>"
            f"</tr></thead><tbody>{factor_rows}</tbody></table></div>"
            f"{_render_matrix_table(data.get('duration_matrix_s') or [], 'Duration Matrix (seconds)')}"
            "<details><summary><strong>Raw Mapbox Response (Before Preprocessing)</strong></summary>"
            f"<pre>{raw_json}</pre></details>"
            "</body></html>"
        )
        resp = HTMLResponse(html_payload)
    else:
        resp = JSONResponse(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/plan/published")
def plan_published(request: Request, date: str | None = None):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    plan_date = date or _business_date(offset_days=0)
    svc = DatabaseService()
    payload = svc.get_plan_payload(plan_date, "published")
    if not payload:
        return JSONResponse({"plan_date": plan_date, "plan_type": "published", "published": False})
    normalized_deliveries = [_normalize_plan_delivery(d) for d in payload.get("deliveries") or []]

    meta = payload.get("meta") or {}
    if isinstance(meta, dict):
        if meta.get("depot_lat") is None:
            meta["depot_lat"] = settings.DEPOT_LAT
        if meta.get("depot_lon") is None:
            meta["depot_lon"] = settings.DEPOT_LON

    resp = JSONResponse(
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
        }
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
    resp = JSONResponse({"items": items})
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

    resp = JSONResponse(
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
        }
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

    resp = JSONResponse(
        {
            "ok": True,
            "record_id": updated.get("id"),
            "scheduled_date": scheduled_date,
        }
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

    resp = JSONResponse(
        {
            "ok": True,
            "record_id": updated.get("id"),
            "delivery_id": delivery_id,
            "ps_delivered": delivered,
        }
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
    resp = JSONResponse({"ok": ok_count == len(results), "count": ok_count, "results": results})
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
    resp = JSONResponse({"ok": True, **result})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.delete("/deliveries/{delivery_id}")
def deliveries_delete(delivery_id: str, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        raise HTTPException(status_code=403, detail="Manager/Admin access required.")
    svc = DatabaseService()
    deleted = svc.delete_record("Deliveries", delivery_id)
    resp = JSONResponse({"ok": True, "deleted": deleted})
    resp.headers["Cache-Control"] = "no-store"
    return resp
