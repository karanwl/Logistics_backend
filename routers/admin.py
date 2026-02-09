import requests
from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from auth import get_current_user, hash_password
from config import settings
from security import csrf_protect, get_csrf_token
from services import DatabaseService, MapboxService
from utils import parse_sales_order, to_int

router = APIRouter(dependencies=[Depends(csrf_protect)])
templates = Jinja2Templates(directory="templates")

def _wants_refresh(request: Request) -> bool:
    refresh = (request.query_params.get("refresh") or "").strip().lower()
    if refresh in {"1", "true", "yes", "y"}:
        return True
    cache_control = (request.headers.get("cache-control") or "").lower()
    pragma = (request.headers.get("pragma") or "").lower()
    return "no-cache" in cache_control or "max-age=0" in cache_control or "no-cache" in pragma


def _template(name: str, context: dict | None = None):
    payload = context or {}
    request = payload.get("request")
    payload.setdefault("csrf_token", get_csrf_token(request) if request else "")
    return templates.TemplateResponse(name, payload)


def _redirect(path: str):
    return RedirectResponse(path, status_code=303)


def _get_next_url(request: Request, next_from_form: str | None = None, fallback: str = "/") -> str:
    if next_from_form:
        return next_from_form
    qp_next = request.query_params.get("next")
    if qp_next:
        return qp_next
    ref = request.headers.get("referer")
    if ref:
        return ref
    return fallback


def _require_user(request: Request):
    user = get_current_user(request)
    if not user:
        return None
    return user


def _require_manager_or_admin(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("manager", "admin"):
        return None
    return user


def _require_admin_only(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return None
    return user


def _to_bool_form(value: str | None) -> bool:
    if value is None:
        return False
    val = str(value).strip().lower()
    return val in {"1", "true", "yes", "y", "on", "checked"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = _require_user(request)
    if not user:
        return _redirect("/login")
    return _template(
        "schedule_board.html",
        {
            "request": request,
            "user": user,
        },
    )


@router.get("/plan", response_class=HTMLResponse)
def plan_view(request: Request):
    user = _require_manager_or_admin(request)
    if not user:
        return _redirect("/")
    return _template(
        "plan.html",
        {
            "request": request,
            "user": user,
            "settings": settings,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user = _require_admin_only(request)
    if not user:
        return _redirect("/")
    return _template(
        "admin_home.html",
        {"request": request, "user": user, "can_edit": True},
    )


@router.get("/meeting-intake", response_class=HTMLResponse)
def admin_meeting_intake(request: Request):
    user = _require_manager_or_admin(request)
    if not user:
        return _redirect("/")
    svc = DatabaseService()
    jobsites = svc.list_jobsites_min(force_refresh=_wants_refresh(request))
    return _template(
        "meeting_intake.html",
        {"request": request, "user": user, "jobsites": jobsites},
    )


@router.get("/users", response_class=HTMLResponse)
def admin_users(request: Request):
    user = _require_admin_only(request)
    if not user:
        return _redirect("/")
    svc = DatabaseService()
    users = svc.list_users_min(active_only=False, force_refresh=_wants_refresh(request))
    roles = ["coordinator", "driver", "manager", "admin"]
    return _template(
        "admin_users.html",
        {"request": request, "user": user, "users": users, "roles": roles, "error": None},
    )


@router.post("/users/create", response_class=HTMLResponse)
def admin_users_create(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("coordinator"),
    active: str | None = Form(None),
):
    user = _require_admin_only(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/users",
                "payload": {"error": "Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )

    role_value = (role or "").strip().lower() or "coordinator"
    roles = ["coordinator", "driver", "manager", "admin"]
    if role_value not in roles:
        svc = DatabaseService()
        users = svc.list_users_min(active_only=False)
        return _template(
            "admin_users.html",
            {
                "request": request,
                "user": user,
                "users": users,
                "roles": roles,
                "error": "Invalid role selected.",
            },
        )

    svc = DatabaseService()
    existing = svc.get_user_by_email(email.strip())
    if existing:
        users = svc.list_users_min(active_only=False)
        return _template(
            "admin_users.html",
            {
                "request": request,
                "user": user,
                "users": users,
                "roles": roles,
                "error": "Email already registered.",
            },
        )

    fields = {
        settings.USERS_EMAIL_FIELD: email.strip(),
        settings.USERS_PASSWORD_FIELD: hash_password(password),
        settings.USERS_ROLE_FIELD: role_value,
        settings.USERS_ACTIVE_FIELD: bool(active),
    }
    if name.strip():
        fields[settings.USERS_NAME_FIELD] = name.strip()

    created = svc.create_user(fields)
    return _template(
        "created.html",
        {
            "request": request,
            "title": "User created",
            "back_href": "/users",
            "payload": created,
            "extra_title": None,
            "extra_payload": None,
        },
    )


@router.post("/users/{record_id}/update", response_class=HTMLResponse)
def admin_users_update(
    request: Request,
    record_id: str,
    name: str = Form(""),
    role: str = Form("coordinator"),
    active: str | None = Form(None),
    password: str = Form(""),
    next_url: str = Form(""),
):
    user = _require_admin_only(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/users",
                "payload": {"error": "Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )

    role_value = (role or "").strip().lower() or "coordinator"
    if role_value not in ("coordinator", "driver", "manager", "admin"):
        svc = DatabaseService()
        users = svc.list_users_min(active_only=False)
        roles = ["coordinator", "driver", "manager", "admin"]
        return _template(
            "admin_users.html",
            {
                "request": request,
                "user": user,
                "users": users,
                "roles": roles,
                "error": "Invalid role selected.",
            },
        )

    fields = {
        settings.USERS_ROLE_FIELD: role_value,
        settings.USERS_ACTIVE_FIELD: bool(active),
        settings.USERS_NAME_FIELD: name.strip() if name.strip() else None,
    }
    if password.strip():
        fields[settings.USERS_PASSWORD_FIELD] = hash_password(password.strip())

    svc = DatabaseService()
    try:
        svc.update_record(settings.USERS_TABLE, record_id, fields)
    except requests.HTTPError as e:
        detail = None
        if e.response is not None:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
        return _template(
            "created.html",
            {
                "request": request,
                "title": "User update failed",
                "back_href": _get_next_url(request, next_url, "/users"),
                "payload": {"record_id": record_id},
                "extra_title": "Airtable error",
                "extra_payload": detail or str(e),
            },
        )

    return _redirect(_get_next_url(request, next_url, "/users"))


@router.get("/published-plans", response_class=HTMLResponse)
def admin_published_plans(request: Request):
    user = _require_manager_or_admin(request)
    if not user:
        return _redirect("/")
    return _template(
        "published_plans.html",
        {"request": request, "user": user, "settings": settings},
    )


@router.get("/jobsites", response_class=HTMLResponse)
def admin_jobsites(request: Request):
    user = _require_manager_or_admin(request)
    if not user:
        return _redirect("/")
    return _template(
        "admin_jobsites.html",
        {
            "request": request,
            "name_field": settings.JOBSITES_NAME_FIELD,
            "address_field": settings.JOBSITES_ADDRESS_FIELD,
            "user": user,
            "error": None,
            "name_value": "",
            "address_value": "",
        },
    )


@router.post("/jobsites/create", response_class=HTMLResponse)
def admin_jobsites_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
):
    user = _require_manager_or_admin(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/jobsites",
                "payload": {"error": "Manager/Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )
    svc = DatabaseService()
    name_clean = name.strip()
    address_clean = address.strip()
    existing = svc.find_jobsite_by_address(address_clean)
    if existing:
        return _template(
            "admin_jobsites.html",
            {
                "request": request,
                "name_field": settings.JOBSITES_NAME_FIELD,
                "address_field": settings.JOBSITES_ADDRESS_FIELD,
                "user": user,
                "error": "Jobsite already exists for this address.",
                "name_value": name_clean,
                "address_value": address_clean,
            },
        )

    # 1) Create jobsite
    created = svc.create_record(
        "Jobsites",
        {
            settings.JOBSITES_NAME_FIELD: name_clean,
            settings.JOBSITES_ADDRESS_FIELD: address_clean,
        },
    )

    # 2) Geocode + update coords (best effort)
    geocode_status = {"success": False, "lat": None, "lon": None, "error": None}
    try:
        mb = MapboxService()
        lat, lon = mb.get_coordinates(address_clean)
        if lat is not None and lon is not None:
            svc.update_record(
                "Jobsites",
                created["id"],
                {settings.JOBSITES_LAT_FIELD: float(lat), settings.JOBSITES_LON_FIELD: float(lon)},
            )
            geocode_status.update({"success": True, "lat": float(lat), "lon": float(lon)})
        else:
            geocode_status["error"] = "Mapbox returned no results for that address."
    except Exception as e:
        geocode_status["error"] = str(e)

    return _template(
        "created.html",
        {
            "request": request,
            "title": "Jobsite created",
            "back_href": "/jobsites",
            "payload": created,
            "extra_title": "Geocode result",
            "extra_payload": geocode_status,
        },
    )


@router.post("/jobsites/create-json")
def admin_jobsites_create_json(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
):
    user = _require_manager_or_admin(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Manager/Admin access required."}, status_code=403)
    svc = DatabaseService()
    name_clean = name.strip()
    address_clean = address.strip()
    existing = svc.find_jobsite_by_address(address_clean)
    if existing:
        fields = existing.get("fields", {})
        return JSONResponse(
            {
                "ok": False,
                "error": "Jobsite already exists for this address.",
                "existing": {
                    "id": existing.get("id"),
                    "name": fields.get(settings.JOBSITES_NAME_FIELD),
                    "address": fields.get(settings.JOBSITES_ADDRESS_FIELD),
                },
            },
            status_code=400,
        )
    created = svc.create_record(
        "Jobsites",
        {
            settings.JOBSITES_NAME_FIELD: name_clean,
            settings.JOBSITES_ADDRESS_FIELD: address_clean,
        },
    )
    geocode_status = {"success": False, "lat": None, "lon": None, "error": None}
    try:
        mb = MapboxService()
        lat, lon = mb.get_coordinates(address.strip())
        if lat is not None and lon is not None:
            svc.update_record(
                "Jobsites",
                created["id"],
                {settings.JOBSITES_LAT_FIELD: float(lat), settings.JOBSITES_LON_FIELD: float(lon)},
            )
            geocode_status.update({"success": True, "lat": float(lat), "lon": float(lon)})
        else:
            geocode_status["error"] = "Mapbox returned no results for that address."
    except Exception as e:
        geocode_status["error"] = str(e)
    return JSONResponse(
        {
            "ok": True,
            "jobsite": {
                "id": created.get("id"),
                "name": name_clean,
                "address": address_clean,
            },
            "geocode": geocode_status,
        }
    )


@router.post("/deliveries/create", response_class=HTMLResponse)
def admin_deliveries_create(
    request: Request,
    jobsite_id: str = Form(...),
    scheduled_date: str = Form(...),
    sales_order: str = Form(""),
    med_skid: str = Form("0"),
    small_skid: str = Form("0"),
    large_skid: str = Form("0"),
    exp_frame: str = Form("0"),
    frame: str = Form("0"),
    door: str = Form("0"),
    box: str = Form("0"),
    pickup: str | None = Form(None),
    requires_empty_truck: str | None = Form(None),
    window_start: str = Form(""),
    window_end: str = Form(""),
    next_url: str = Form(""),
):
    user = _require_manager_or_admin(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/",
                "payload": {"error": "Manager/Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )
    svc = DatabaseService()

    fields = {
        settings.DELIVERIES_JOB_LINK_FIELD: [jobsite_id],
        "Scheduled Date": scheduled_date.strip(),
        "Med Skid": to_int(med_skid),
        "Small Skid": to_int(small_skid),
        "Large Skid": to_int(large_skid),
        "Exp Frame": to_int(exp_frame),
        "Frame": to_int(frame),
        "Door": to_int(door),
        "Box": to_int(box),
        "Pickup": _to_bool_form(pickup),
        "Requires Empty Truck": _to_bool_form(requires_empty_truck),
    }
    sales_order_value = parse_sales_order(sales_order)
    if sales_order_value is not None:
        fields[settings.DELIVERIES_SALES_ORDER_FIELD] = sales_order_value

    if window_start.strip():
        fields["Window Start"] = window_start.strip()
    if window_end.strip():
        fields["Window End"] = window_end.strip()

    created = svc.create_record(
        "Deliveries",
        fields,
    )

    return _redirect(_get_next_url(request, next_url, "/"))


@router.get("/deliveries/{delivery_id}", response_class=HTMLResponse)
def admin_deliveries_edit(request: Request, delivery_id: str):
    user = _require_manager_or_admin(request)
    if not user:
        return _redirect("/")
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Delivery not found",
                "back_href": "/",
                "payload": {"delivery_id": delivery_id},
                "extra_title": None,
                "extra_payload": None,
            },
        )

    fields = rec.get("fields", {})
    jobsites = svc.list_jobsites_min(force_refresh=_wants_refresh(request))
    jobsite_ids = fields.get(settings.DELIVERIES_JOB_LINK_FIELD) or []
    jobsite_id = jobsite_ids[0] if isinstance(jobsite_ids, list) and jobsite_ids else None

    return _template(
        "admin_delivery_edit.html",
        {
            "request": request,
            "jobsites": jobsites,
            "job_link_field": settings.DELIVERIES_JOB_LINK_FIELD,
            "delivery": {
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
            },
            "user": user,
            "next_url": request.query_params.get("next") or "/",
            "embed": request.query_params.get("embed") == "1",
        },
    )


@router.post("/deliveries/{delivery_id}/update", response_class=HTMLResponse)
def admin_deliveries_update(
    request: Request,
    delivery_id: str,
    jobsite_id: str = Form(...),
    scheduled_date: str = Form(...),
    sales_order: str = Form(""),
    med_skid: str = Form("0"),
    small_skid: str = Form("0"),
    large_skid: str = Form("0"),
    exp_frame: str = Form("0"),
    frame: str = Form("0"),
    door: str = Form("0"),
    box: str = Form("0"),
    pickup: str | None = Form(None),
    requires_empty_truck: str | None = Form(None),
    window_start: str = Form(""),
    window_end: str = Form(""),
    next_url: str = Form(""),
):
    user = _require_manager_or_admin(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/",
                "payload": {"error": "Manager/Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Delivery not found",
                "back_href": "/",
                "payload": {"delivery_id": delivery_id},
                "extra_title": None,
                "extra_payload": None,
            },
        )

    fields = {
        settings.DELIVERIES_JOB_LINK_FIELD: [jobsite_id],
        "Scheduled Date": scheduled_date.strip(),
        "Med Skid": to_int(med_skid),
        "Small Skid": to_int(small_skid),
        "Large Skid": to_int(large_skid),
        "Exp Frame": to_int(exp_frame),
        "Frame": to_int(frame),
        "Door": to_int(door),
        "Box": to_int(box),
        "Pickup": _to_bool_form(pickup),
        "Requires Empty Truck": _to_bool_form(requires_empty_truck),
    }
    sales_order_value = parse_sales_order(sales_order)
    if sales_order_value is not None:
        fields[settings.DELIVERIES_SALES_ORDER_FIELD] = sales_order_value
    else:
        fields[settings.DELIVERIES_SALES_ORDER_FIELD] = None

    if window_start.strip():
        fields["Window Start"] = window_start.strip()
    else:
        fields["Window Start"] = None

    if window_end.strip():
        fields["Window End"] = window_end.strip()
    else:
        fields["Window End"] = None

    try:
        updated = svc.update_record("Deliveries", rec.get("id"), fields)
    except requests.HTTPError as e:
        detail = None
        if e.response is not None:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Delivery update failed",
                "back_href": _get_next_url(request, next_url, "/"),
                "payload": {"delivery_id": delivery_id},
                "extra_title": "Airtable error",
                "extra_payload": detail or str(e),
            },
        )

    return _redirect(_get_next_url(request, next_url, "/"))


@router.post("/deliveries/{delivery_id}/delete", response_class=HTMLResponse)
def admin_deliveries_delete(request: Request, delivery_id: str):
    user = _require_manager_or_admin(request)
    if not user:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Access denied",
                "back_href": "/",
                "payload": {"error": "Manager/Admin access required."},
                "extra_title": None,
                "extra_payload": None,
            },
        )
    svc = DatabaseService()
    rec = svc.get_delivery_by_business_id(delivery_id)
    if not rec:
        return _template(
            "created.html",
            {
                "request": request,
                "title": "Delivery not found",
                "back_href": "/",
                "payload": {"delivery_id": delivery_id},
                "extra_title": None,
                "extra_payload": None,
            },
        )

    deleted = svc.delete_record("Deliveries", rec.get("id"))

    return _redirect(_get_next_url(request, None, "/"))
