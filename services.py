import hashlib
import copy
import json
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db import SessionLocal, engine
from db_models import Delivery, Jobsite, PublishedPlan, User
from models import DeliveryJob

logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()
_TABLE_CACHE: dict[str, list[dict]] = {}
_MATRIX_CACHE_LOCK = threading.Lock()


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=settings.HTTP_RETRIES,
        backoff_factor=settings.HTTP_RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "PATCH", "DELETE"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_HTTP_SESSION = _build_session()

class DatabaseService:
    """
    Postgres service (SQLAlchemy).
    Supports:
      - list jobsites (for dropdown)
      - create jobsite (name/address)
      - create delivery with linked jobsite
      - read tomorrow deliveries, resolving jobsite name/address (with fallbacks)
    """

    def __init__(self):
        if not settings.DATABASE_URL:
            raise RuntimeError("Missing DATABASE_URL env var.")

    _published_plan_type_supported: bool | None = None
    _live_views_supported: bool | None = None
    _normalized_schema_supported: bool | None = None

    @classmethod
    def _supports_published_plan_type(cls, bind=None) -> bool:
        if cls._published_plan_type_supported is not None:
            return cls._published_plan_type_supported
        try:
            inspector = inspect(bind or engine)
            table_names = set(inspector.get_table_names())
            if "published_plans" in table_names:
                cols = inspector.get_columns("published_plans")
                col_names = {str(c.get("name", "")).strip() for c in cols}
                cls._published_plan_type_supported = "plan_type" in col_names
            else:
                cls._published_plan_type_supported = False
        except Exception:
            cls._published_plan_type_supported = False
        return cls._published_plan_type_supported

    @staticmethod
    def _legacy_published_plan_has_type(bind=None) -> bool:
        try:
            inspector = inspect(bind or engine)
            table_names = set(inspector.get_table_names())
            if "Published Plans" not in table_names:
                return False
            cols = inspector.get_columns("Published Plans")
            col_names = {str(c.get("name", "")).strip() for c in cols}
            return "Plan Type" in col_names
        except Exception:
            return False

    @classmethod
    def _supports_live_views(cls, bind=None) -> bool:
        if cls._live_views_supported is True:
            return True
        try:
            with (bind.connect() if bind is not None and hasattr(bind, "connect") else engine.connect()) as conn:
                exists = conn.execute(
                    text("SELECT to_regclass('public.v_route_live')")
                ).scalar_one_or_none()
            cls._live_views_supported = bool(exists)
        except Exception:
            cls._live_views_supported = False
        return cls._live_views_supported

    @classmethod
    def _uses_normalized_schema(cls, bind=None) -> bool:
        if cls._normalized_schema_supported is True:
            return True
        try:
            inspector = inspect(bind or engine)
            tables = {str(name) for name in inspector.get_table_names()}
            cls._normalized_schema_supported = {"users", "jobsites", "deliveries"}.issubset(tables)
        except Exception:
            cls._normalized_schema_supported = False
        return cls._normalized_schema_supported

    @staticmethod
    def _table_exists(table_name: str, bind=None) -> bool:
        try:
            inspector = inspect(bind or engine)
            return str(table_name) in set(inspector.get_table_names())
        except Exception:
            return False

    @staticmethod
    def _format_time_value(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:5] if len(text) >= 5 else text

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _normalize_delivery_status(value: Any) -> str | None:
        raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not raw:
            return None
        allowed = {"pending", "assigned", "en_route", "arrived", "delivered", "failed", "cancelled"}
        if raw == "in_progress":
            return "en_route"
        if raw == "canceled":
            return "cancelled"
        return raw if raw in allowed else None

    @staticmethod
    def _normalize_table_name(table_name: str) -> str:
        return (table_name or "").strip()

    @classmethod
    def _is_jobsites_table(cls, table_name: str) -> bool:
        raw = cls._normalize_table_name(table_name).lower()
        return raw in {"jobsites"}

    @classmethod
    def _is_deliveries_table(cls, table_name: str) -> bool:
        raw = cls._normalize_table_name(table_name).lower()
        return raw in {"deliveries"}

    @classmethod
    def _is_users_table(cls, table_name: str) -> bool:
        raw = cls._normalize_table_name(table_name).lower()
        settings_name = cls._normalize_table_name(settings.USERS_TABLE).lower()
        return raw in {"users", settings_name}

    @classmethod
    def _is_published_plans_table(cls, table_name: str) -> bool:
        raw = cls._normalize_table_name(table_name).lower()
        settings_name = cls._normalize_table_name(settings.PUBLISHED_PLANS_TABLE).lower()
        return raw in {"published_plans", "published plans", settings_name}

    @classmethod
    def _get_cached_table(cls, table_name: str) -> list[dict] | None:
        key = cls._normalize_table_name(table_name)
        if not key:
            return None
        with _CACHE_LOCK:
            cached = _TABLE_CACHE.get(key)
            return cached

    @classmethod
    def _set_cached_table(cls, table_name: str, data: list[dict]) -> None:
        key = cls._normalize_table_name(table_name)
        if not key:
            return
        with _CACHE_LOCK:
            _TABLE_CACHE[key] = data

    @classmethod
    def _invalidate_cache(cls, table_name: str | None = None) -> None:
        with _CACHE_LOCK:
            if not table_name:
                _TABLE_CACHE.clear()
                return
            key = cls._normalize_table_name(table_name)
            _TABLE_CACHE.pop(key, None)

    @staticmethod
    def _first_str(v) -> str:
        if isinstance(v, list):
            return str(v[0]) if v else ""
        if v is None:
            return ""
        return str(v)

    @staticmethod
    def _to_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, list):
            return bool(v)
        val = str(v).strip().lower()
        return val in ("true", "yes", "1", "checked", "y")

    @staticmethod
    def _normalize_pickup_vehicle(value) -> str:
        if value is None:
            return ""
        raw = str(value).strip().lower()
        if not raw:
            return ""
        if raw in ("van", "vans", "van-only", "van only", "van_only"):
            return "van"
        if raw in ("truck", "trucks", "truck-only", "truck only", "truck_only"):
            return "truck"
        if raw in ("both", "any", "either", "all", "van/truck", "truck/van"):
            return "both"
        return ""

    @staticmethod
    def _normalize_address(value: str) -> str:
        if not value:
            return ""
        text = str(value).lower().strip()
        text = re.sub(r"[.,]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _parse_airtable_date(value) -> date | None:
        if not value:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.date()
        except Exception:
            return None

    @staticmethod
    def _coerce_int(value) -> int | None:
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _coerce_float(value) -> float | None:
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _coerce_date(value) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        try:
            return date.fromisoformat(str(value))
        except Exception:
            return None

    @staticmethod
    def _normalize_email(value: str | None) -> str:
        return str(value or "").strip().lower()

    def _jobsite_record(self, jobsite: Jobsite) -> Dict[str, Any]:
        fields = {
            "Job ID": jobsite.job_id,
            settings.JOBSITES_NAME_FIELD: jobsite.name,
            settings.JOBSITES_ADDRESS_FIELD: jobsite.address,
            settings.JOBSITES_LAT_FIELD: jobsite.latitude,
            settings.JOBSITES_LON_FIELD: jobsite.longitude,
            "Site Contact": jobsite.site_contact,
            "Site Contact Number": jobsite.site_contact_number,
        }
        return {"id": str(jobsite.job_id), "fields": fields}

    def _delivery_record(self, delivery: Delivery) -> Dict[str, Any]:
        scheduled = delivery.scheduled_date.isoformat() if delivery.scheduled_date else None
        job_id = delivery.job_id
        job_ids = [str(job_id)] if job_id is not None else []
        status_updated_at = (
            delivery.status_updated_at.isoformat()
            if isinstance(delivery.status_updated_at, datetime)
            else delivery.status_updated_at
        )
        arrived_at = (
            delivery.arrived_at.isoformat()
            if isinstance(delivery.arrived_at, datetime)
            else delivery.arrived_at
        )
        delivered_at = (
            delivery.delivered_at.isoformat()
            if isinstance(delivery.delivered_at, datetime)
            else delivery.delivered_at
        )
        updated_at = (
            delivery.updated_at.isoformat()
            if isinstance(getattr(delivery, "updated_at", None), datetime)
            else getattr(delivery, "updated_at", None)
        )
        fields = {
            "Delivery ID": delivery.delivery_id,
            settings.DELIVERIES_JOB_LINK_FIELD: job_ids,
            settings.DELIVERIES_SALES_ORDER_FIELD: delivery.sales_order,
            settings.DELIVERIES_PS_DELIVERED_FIELD: bool(delivery.ps_delivered) if delivery.ps_delivered is not None else False,
            "Scheduled Date": scheduled,
            "Med Skid": delivery.med_skid,
            "Small Skid": delivery.small_skid,
            "Large Skid": delivery.large_skid,
            "Door": delivery.door,
            "Frame": delivery.frame,
            "Exp Frame": delivery.exp_frame,
            "Box": delivery.box,
            "Pickup": bool(delivery.pickup) if delivery.pickup is not None else False,
            settings.DELIVERIES_PICKUP_VEHICLE_FIELD: (
                str(getattr(delivery, "pickup_vehicle", "") or "").strip() or None
            ),
            "Requires Empty Truck": bool(delivery.requires_empty_truck) if delivery.requires_empty_truck is not None else False,
            settings.DELIVERIES_DRIVER_FIELD: delivery.drivers,
            settings.DELIVERIES_STATUS_FIELD: getattr(delivery, "status", None),
            settings.DELIVERIES_STATUS_UPDATED_FIELD: status_updated_at,
            settings.DELIVERIES_ARRIVED_AT_FIELD: arrived_at,
            settings.DELIVERIES_DELIVERED_AT_FIELD: delivered_at,
            settings.DELIVERIES_DRIVER_LAT_FIELD: getattr(delivery, "driver_latitude", None),
            settings.DELIVERIES_DRIVER_LON_FIELD: getattr(delivery, "driver_longitude", None),
            settings.DELIVERIES_DRIVER_NOTES_FIELD: delivery.delivery_notes,
            "Window Start": delivery.window_start,
            "Window End": delivery.window_end,
            settings.DELIVERIES_PACKING_SLIP_FIELD: delivery.packing_slip,
            "Route Sequence": getattr(delivery, "route_sequence", None),
            "Updated At": updated_at,
        }
        return {"id": str(delivery.delivery_id), "fields": fields}

    def _user_record(self, user: User) -> Dict[str, Any]:
        session_expires_value = getattr(user, "session_expires", None)
        session_expires = (
            session_expires_value.isoformat()
            if isinstance(session_expires_value, datetime)
            else session_expires_value
        )
        name_value = getattr(user, "name", None)
        if not name_value:
            name_value = getattr(user, "full_name", None)
        session_token_value = getattr(user, "session_token", None)
        fields = {
            settings.USERS_EMAIL_FIELD: user.email,
            settings.USERS_PASSWORD_FIELD: user.password_hash,
            settings.USERS_ROLE_FIELD: user.role,
            settings.USERS_ACTIVE_FIELD: user.active,
            settings.USERS_NAME_FIELD: name_value,
            settings.USERS_SESSION_TOKEN_FIELD: session_token_value,
            settings.USERS_SESSION_EXPIRES_FIELD: session_expires,
        }
        return {"id": user.email, "fields": fields}

    def _legacy_user_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        raw_email = self._first_str(row.get("Email")).strip()
        normalized_email = self._normalize_email(raw_email) or raw_email
        session_expires_value = row.get("Session Expires")
        if isinstance(session_expires_value, datetime):
            session_expires = session_expires_value.isoformat()
        else:
            session_expires_text = self._first_str(session_expires_value).strip()
            session_expires = session_expires_text or None
        fields = {
            settings.USERS_EMAIL_FIELD: normalized_email,
            settings.USERS_PASSWORD_FIELD: row.get("Password Hash"),
            settings.USERS_ROLE_FIELD: row.get("Role"),
            settings.USERS_ACTIVE_FIELD: self._to_bool(row.get("Active", True)),
            settings.USERS_NAME_FIELD: row.get("Name"),
            settings.USERS_SESSION_TOKEN_FIELD: row.get("Session Token"),
            settings.USERS_SESSION_EXPIRES_FIELD: session_expires,
        }
        return {"id": normalized_email, "fields": fields}

    @staticmethod
    def _users_column_names(bind=None) -> set[str]:
        try:
            inspector = inspect(bind or engine)
            if "users" not in set(inspector.get_table_names()):
                return set()
            cols = inspector.get_columns("users")
            return {str(col.get("name", "")).strip() for col in cols}
        except Exception:
            return set()

    def _user_record_from_users_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        email_raw = row.get("email")
        email = self._normalize_email(email_raw) or self._first_str(email_raw)
        session_expires_value = row.get("session_expires")
        if isinstance(session_expires_value, datetime):
            session_expires = session_expires_value.isoformat()
        else:
            session_expires_text = self._first_str(session_expires_value).strip()
            session_expires = session_expires_text or None

        name_value = row.get("name")
        if not name_value:
            name_value = row.get("full_name")

        active_raw = row.get("active")
        active_value = True if active_raw is None else self._to_bool(active_raw)

        fields = {
            settings.USERS_EMAIL_FIELD: email,
            settings.USERS_PASSWORD_FIELD: row.get("password_hash"),
            settings.USERS_ROLE_FIELD: row.get("role"),
            settings.USERS_ACTIVE_FIELD: active_value,
            settings.USERS_NAME_FIELD: name_value,
            settings.USERS_SESSION_TOKEN_FIELD: row.get("session_token"),
            settings.USERS_SESSION_EXPIRES_FIELD: session_expires,
        }
        return {"id": email, "fields": fields}

    @staticmethod
    def _users_select_sql() -> str:
        return """
            SELECT
                "Email",
                "Password Hash",
                "Role",
                COALESCE("Active", TRUE) AS "Active",
                "Name",
                "Session Token",
                "Session Expires"
            FROM "Users"
        """

    def _query_legacy_user_by_email(self, session, normalized_email: str) -> Dict[str, Any] | None:
        row = session.execute(
            text(
                f"""
                {self._users_select_sql()}
                WHERE lower(btrim("Email")) = :email
                LIMIT 1
                """
            ),
            {"email": normalized_email},
        ).mappings().first()
        if not row:
            return None
        return self._legacy_user_record(dict(row))

    def _query_legacy_user_by_session_token(self, session, token: str) -> Dict[str, Any] | None:
        row = session.execute(
            text(
                f"""
                {self._users_select_sql()}
                WHERE "Session Token" = :token
                LIMIT 1
                """
            ),
            {"token": token},
        ).mappings().first()
        if not row:
            return None
        return self._legacy_user_record(dict(row))

    @staticmethod
    def _normalize_plan_type(value: str | None, default: str = "published") -> str:
        normalized = str(value or "").strip().lower()
        return normalized or default

    def _plan_record(self, plan: PublishedPlan) -> Dict[str, Any]:
        plan_date = plan.plan_date.isoformat() if plan.plan_date else None
        plan_type = self._normalize_plan_type(getattr(plan, "plan_type", None))
        published_at = (
            plan.published_at.isoformat()
            if isinstance(plan.published_at, datetime)
            else plan.published_at
        )
        fields = {
            settings.PUBLISHED_PLANS_DATE_FIELD: plan_date,
            settings.PUBLISHED_PLANS_TYPE_FIELD: plan_type,
            settings.PUBLISHED_PLANS_FLEET_FIELD: plan.fleet_json,
            settings.PUBLISHED_PLANS_ROUTES_FIELD: plan.routes_json,
            settings.PUBLISHED_PLANS_DELIVERIES_FIELD: plan.deliveries_json,
            settings.PUBLISHED_PLANS_META_FIELD: plan.meta_json,
            settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: plan.driver_profiles_json,
            settings.PUBLISHED_PLANS_STATUS_FIELD: plan.status,
            settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD: plan.published_by,
            settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: published_at,
        }
        record_id = f"{plan_date}::{plan_type}" if plan_date else ""
        return {"id": record_id, "fields": fields, "createdTime": published_at}

    def _legacy_plan_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        plan_date_val = row.get("Plan Date")
        plan_date = plan_date_val.isoformat() if hasattr(plan_date_val, "isoformat") else str(plan_date_val or "")
        plan_type = self._normalize_plan_type(row.get("Plan Type"), default="published")
        fields = {
            settings.PUBLISHED_PLANS_DATE_FIELD: plan_date,
            settings.PUBLISHED_PLANS_TYPE_FIELD: plan_type,
            settings.PUBLISHED_PLANS_FLEET_FIELD: row.get("Fleet JSON"),
            settings.PUBLISHED_PLANS_ROUTES_FIELD: row.get("Routes JSON"),
            settings.PUBLISHED_PLANS_DELIVERIES_FIELD: row.get("Deliveries JSON"),
            settings.PUBLISHED_PLANS_META_FIELD: row.get("Meta JSON"),
            settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: row.get("Driver Profiles JSON"),
            settings.PUBLISHED_PLANS_STATUS_FIELD: row.get("Status"),
            settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD: row.get("Published By"),
            settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: row.get("Published At"),
        }
        record_id = f"{plan_date}::{plan_type}" if plan_type != "published" else plan_date
        return {"id": record_id, "fields": fields, "createdTime": row.get("Published At")}

    def _plan_payload(self, plan: PublishedPlan) -> Dict[str, Any]:
        plan_date = plan.plan_date.isoformat() if plan.plan_date else None
        plan_type = self._normalize_plan_type(getattr(plan, "plan_type", None))
        updated_at = (
            plan.published_at.isoformat()
            if isinstance(plan.published_at, datetime)
            else plan.published_at
        )
        return {
            "plan_date": plan_date,
            "plan_type": plan_type,
            "routes": self._json_load(plan.routes_json, []),
            "deliveries": self._json_load(plan.deliveries_json, []),
            "meta": self._json_load(plan.meta_json, {}),
            "fleet": self._json_load(plan.fleet_json, {}),
            "driver_profiles": self._json_load(plan.driver_profiles_json, []),
            "status": plan.status,
            "updated_at": updated_at,
            "updated_by": plan.published_by,
        }

    def _legacy_plan_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        plan_date_val = row.get("Plan Date")
        plan_date = plan_date_val.isoformat() if hasattr(plan_date_val, "isoformat") else str(plan_date_val or "")
        plan_type = self._normalize_plan_type(row.get("Plan Type"), default="published")
        return {
            "plan_date": plan_date,
            "plan_type": plan_type,
            "routes": self._json_load(row.get("Routes JSON"), []),
            "deliveries": self._json_load(row.get("Deliveries JSON"), []),
            "meta": self._json_load(row.get("Meta JSON"), {}),
            "fleet": self._json_load(row.get("Fleet JSON"), {}),
            "driver_profiles": self._json_load(row.get("Driver Profiles JSON"), []),
            "status": row.get("Status"),
            "updated_at": row.get("Published At"),
            "updated_by": row.get("Published By"),
        }

    def _apply_plan_payload_fields(self, plan: PublishedPlan, fields: Dict[str, Any]) -> None:
        if "routes" in fields:
            plan.routes_json = self._json_dumps(fields.get("routes", []))
        if "deliveries" in fields:
            plan.deliveries_json = self._json_dumps(fields.get("deliveries", []))
        if "meta" in fields:
            plan.meta_json = self._json_dumps(fields.get("meta", {}))
        if "fleet" in fields:
            plan.fleet_json = self._json_dumps(fields.get("fleet", {}))
        if "driver_profiles" in fields:
            plan.driver_profiles_json = self._json_dumps(fields.get("driver_profiles", []))
        if "status" in fields:
            plan.status = self._first_str(fields.get("status")) if fields.get("status") not in (None, "") else None
        if "updated_at" in fields:
            plan.published_at = self._coerce_datetime(fields.get("updated_at"))
        if "updated_by" in fields:
            plan.published_by = fields.get("updated_by")

    def list_plan_meta(self, plan_type: str) -> List[Dict[str, Any]]:
        normalized_type = self._normalize_plan_type(plan_type)
        with SessionLocal() as session:
            if not self._supports_published_plan_type(session.bind):
                if not self._table_exists("Published Plans", session.bind):
                    return []
                has_legacy_type = self._legacy_published_plan_has_type(session.bind)
                if normalized_type not in {"published", "all", "*"} and not has_legacy_type:
                    return []
                params: Dict[str, Any] = {}
                type_expr = (
                    "COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') AS \"Plan Type\""
                    if has_legacy_type
                    else "'published' AS \"Plan Type\""
                )
                where_clause = ""
                if normalized_type not in {"all", "*"}:
                    if has_legacy_type:
                        where_clause = "WHERE COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') = :plan_type"
                        params["plan_type"] = normalized_type
                    elif normalized_type != "published":
                        return []
                rows = session.execute(
                    text(
                        f"""
                        SELECT
                            "Plan Date",
                            {type_expr},
                            "Published At",
                            "Published By"
                        FROM "Published Plans"
                        {where_clause}
                        ORDER BY "Plan Date" DESC
                        """
                    ),
                    params,
                ).mappings().all()
                return [
                    {
                        "plan_date": row.get("Plan Date").isoformat() if hasattr(row.get("Plan Date"), "isoformat") else str(row.get("Plan Date") or ""),
                        "plan_type": self._normalize_plan_type(row.get("Plan Type"), default="published"),
                        "updated_at": row.get("Published At"),
                        "updated_by": row.get("Published By"),
                    }
                    for row in rows
                ]
            stmt = select(PublishedPlan).where(PublishedPlan.plan_type == normalized_type)
            if normalized_type == "published":
                stmt = select(PublishedPlan).where(
                    (PublishedPlan.plan_type == normalized_type) | (PublishedPlan.plan_type.is_(None))
                )
            stmt = stmt.order_by(PublishedPlan.plan_date.desc())
            records = session.execute(stmt).scalars().all()
            return [
                {
                    "plan_date": rec.plan_date.isoformat() if rec.plan_date else None,
                    "plan_type": self._normalize_plan_type(rec.plan_type),
                    "updated_at": rec.published_at,
                    "updated_by": rec.published_by,
                }
                for rec in records
            ]

    def get_plan_payload(self, plan_date: str, plan_type: str) -> Dict[str, Any] | None:
        plan_dt = self._coerce_date(plan_date)
        if not plan_dt:
            return None
        normalized_type = self._normalize_plan_type(plan_type)
        with SessionLocal() as session:
            if not self._supports_published_plan_type(session.bind):
                if not self._table_exists("Published Plans", session.bind):
                    return None
                has_legacy_type = self._legacy_published_plan_has_type(session.bind)
                if normalized_type != "published" and not has_legacy_type:
                    return None
                params: Dict[str, Any] = {"plan_date": plan_dt}
                type_expr = (
                    "COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') AS \"Plan Type\""
                    if has_legacy_type
                    else "'published' AS \"Plan Type\""
                )
                where_extra = ""
                if has_legacy_type:
                    where_extra = "AND COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') = :plan_type"
                    params["plan_type"] = normalized_type
                row = session.execute(
                    text(
                        f"""
                        SELECT
                            "Plan Date",
                            {type_expr},
                            "Fleet JSON",
                            "Routes JSON",
                            "Deliveries JSON",
                            "Meta JSON",
                            "Driver Profiles JSON",
                            "Status",
                            "Published By",
                            "Published At"
                        FROM "Published Plans"
                        WHERE "Plan Date" = :plan_date
                          {where_extra}
                        """
                    ),
                    params,
                ).mappings().first()
                if not row:
                    return None
                return self._legacy_plan_payload(dict(row))
            stmt = (
                select(PublishedPlan)
                .where(PublishedPlan.plan_date == plan_dt, PublishedPlan.plan_type == normalized_type)
            )
            record = session.execute(stmt).scalar_one_or_none()
            if not record:
                return None
            return self._plan_payload(record)

    def upsert_plan_payload(self, plan_date: str, plan_type: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        plan_dt = self._coerce_date(plan_date)
        if not plan_dt:
            raise ValueError("Invalid plan_date.")
        normalized_type = self._normalize_plan_type(plan_type)
        with SessionLocal() as session:
            if not self._supports_published_plan_type(session.bind):
                if not self._table_exists("Published Plans", session.bind):
                    raise ValueError("Published plans storage is not configured.")
                has_legacy_type = self._legacy_published_plan_has_type(session.bind)
                if normalized_type != "published" and not has_legacy_type:
                    raise ValueError(
                        'Published Plans schema is missing "Plan Type". Add the column before saving plan records.'
                    )
                params = {
                    "plan_date": plan_dt,
                    "plan_type": normalized_type,
                    "fleet_json": self._json_dumps(fields.get("fleet", {})),
                    "routes_json": self._json_dumps(fields.get("routes", [])),
                    "deliveries_json": self._json_dumps(fields.get("deliveries", [])),
                    "meta_json": self._json_dumps(fields.get("meta", {})),
                    "driver_profiles_json": self._json_dumps(fields.get("driver_profiles", [])),
                    "status": self._first_str(fields.get("status")) if fields.get("status") not in (None, "") else None,
                    "published_by": fields.get("updated_by"),
                    "published_at": fields.get("updated_at"),
                }
                if has_legacy_type:
                    session.execute(
                        text(
                            """
                            INSERT INTO "Published Plans"
                                ("Plan Date", "Plan Type", "Fleet JSON", "Routes JSON", "Deliveries JSON", "Meta JSON",
                                 "Driver Profiles JSON", "Status", "Published By", "Published At")
                            VALUES
                                (:plan_date, :plan_type, :fleet_json, :routes_json, :deliveries_json, :meta_json,
                                 :driver_profiles_json, :status, :published_by, :published_at)
                            ON CONFLICT ("Plan Date", "Plan Type") DO UPDATE SET
                                "Fleet JSON" = EXCLUDED."Fleet JSON",
                                "Routes JSON" = EXCLUDED."Routes JSON",
                                "Deliveries JSON" = EXCLUDED."Deliveries JSON",
                                "Meta JSON" = EXCLUDED."Meta JSON",
                                "Driver Profiles JSON" = EXCLUDED."Driver Profiles JSON",
                                "Status" = EXCLUDED."Status",
                                "Published By" = EXCLUDED."Published By",
                                "Published At" = EXCLUDED."Published At"
                            """
                        ),
                        params,
                    )
                else:
                    session.execute(
                        text(
                            """
                            INSERT INTO "Published Plans"
                                ("Plan Date", "Fleet JSON", "Routes JSON", "Deliveries JSON", "Meta JSON",
                                 "Driver Profiles JSON", "Status", "Published By", "Published At")
                            VALUES
                                (:plan_date, :fleet_json, :routes_json, :deliveries_json, :meta_json,
                                 :driver_profiles_json, :status, :published_by, :published_at)
                            ON CONFLICT ("Plan Date") DO UPDATE SET
                                "Fleet JSON" = EXCLUDED."Fleet JSON",
                                "Routes JSON" = EXCLUDED."Routes JSON",
                                "Deliveries JSON" = EXCLUDED."Deliveries JSON",
                                "Meta JSON" = EXCLUDED."Meta JSON",
                                "Driver Profiles JSON" = EXCLUDED."Driver Profiles JSON",
                                "Status" = EXCLUDED."Status",
                                "Published By" = EXCLUDED."Published By",
                                "Published At" = EXCLUDED."Published At"
                            """
                        ),
                        params,
                    )
                session.commit()
                where_extra = ""
                query_params: Dict[str, Any] = {"plan_date": plan_dt}
                type_expr = (
                    "COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') AS \"Plan Type\""
                    if has_legacy_type
                    else "'published' AS \"Plan Type\""
                )
                if has_legacy_type:
                    where_extra = "AND COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') = :plan_type"
                    query_params["plan_type"] = normalized_type
                row = session.execute(
                    text(
                        f"""
                        SELECT
                            "Plan Date",
                            {type_expr},
                            "Fleet JSON",
                            "Routes JSON",
                            "Deliveries JSON",
                            "Meta JSON",
                            "Driver Profiles JSON",
                            "Status",
                            "Published By",
                            "Published At"
                        FROM "Published Plans"
                        WHERE "Plan Date" = :plan_date
                          {where_extra}
                        """
                    ),
                    query_params,
                ).mappings().first()
                if not row:
                    raise ValueError("Failed to save published plan.")
                self._invalidate_cache(settings.PUBLISHED_PLANS_TABLE)
                return self._legacy_plan_payload(dict(row))
            stmt = (
                select(PublishedPlan)
                .where(PublishedPlan.plan_date == plan_dt, PublishedPlan.plan_type == normalized_type)
            )
            plan = session.execute(stmt).scalar_one_or_none()
            if not plan:
                plan = PublishedPlan(plan_date=plan_dt, plan_type=normalized_type)
                session.add(plan)
            self._apply_plan_payload_fields(plan, fields)
            if hasattr(plan, "updated_at"):
                plan.updated_at = datetime.now(ZoneInfo("UTC"))
            session.commit()
            session.refresh(plan)
            self._invalidate_cache(settings.PUBLISHED_PLANS_TABLE)
            return self._plan_payload(plan)

    def _apply_delivery_fields(self, delivery: Delivery, fields: Dict[str, Any]) -> None:
        for key, value in (fields or {}).items():
            if key == settings.DELIVERIES_JOB_LINK_FIELD or key == "Job ID":
                delivery.job_id = self._coerce_int(value)
            elif key == "Delivery ID":
                delivery.delivery_id = self._coerce_int(value) or delivery.delivery_id
            elif key == settings.DELIVERIES_SALES_ORDER_FIELD:
                delivery.sales_order = self._coerce_int(value)
            elif key == settings.DELIVERIES_PS_DELIVERED_FIELD:
                delivery.ps_delivered = self._to_bool(value)
            elif key == "Scheduled Date":
                delivery.scheduled_date = self._coerce_date(value)
            elif key == "Med Skid":
                delivery.med_skid = self._coerce_int(value)
            elif key == "Skid":
                delivery.med_skid = self._coerce_int(value)
            elif key == "Small Skid":
                delivery.small_skid = self._coerce_int(value)
            elif key == "Large Skid":
                delivery.large_skid = self._coerce_int(value)
            elif key == "Pickup":
                delivery.pickup = self._to_bool(value)
            elif key == settings.DELIVERIES_PICKUP_VEHICLE_FIELD or key == "Pickup Vehicle":
                delivery.pickup_vehicle = self._normalize_pickup_vehicle(value)
            elif key == "Requires Empty Truck":
                delivery.requires_empty_truck = self._to_bool(value)
            elif key == "Door":
                delivery.door = self._coerce_int(value)
            elif key == "Frame":
                delivery.frame = self._coerce_int(value)
            elif key == "Exp Frame":
                delivery.exp_frame = self._coerce_int(value)
            elif key == "Box":
                delivery.box = self._coerce_int(value)
            elif key == settings.DELIVERIES_DRIVER_FIELD:
                delivery.drivers = self._first_str(value) if value is not None else None
            elif key == settings.DELIVERIES_STATUS_FIELD or key == "Status":
                delivery.status = self._first_str(value) if value not in (None, "") else None
            elif key == settings.DELIVERIES_STATUS_UPDATED_FIELD or key == "Status Updated At":
                delivery.status_updated_at = self._coerce_datetime(value)
            elif key == settings.DELIVERIES_ARRIVED_AT_FIELD or key == "Arrived At":
                delivery.arrived_at = self._coerce_datetime(value)
            elif key == settings.DELIVERIES_DELIVERED_AT_FIELD or key == "Delivered At":
                delivery.delivered_at = self._coerce_datetime(value)
            elif key == settings.DELIVERIES_DRIVER_LAT_FIELD or key == "Driver Latitude":
                delivery.driver_latitude = self._coerce_float(value)
            elif key == settings.DELIVERIES_DRIVER_LON_FIELD or key == "Driver Longitude":
                delivery.driver_longitude = self._coerce_float(value)
            elif key == settings.DELIVERIES_DRIVER_NOTES_FIELD:
                delivery.delivery_notes = self._first_str(value) if value is not None else None
            elif key == "Window Start":
                delivery.window_start = self._first_str(value) if value not in (None, "") else None
            elif key == "Window End":
                delivery.window_end = self._first_str(value) if value not in (None, "") else None
            elif key == settings.DELIVERIES_PACKING_SLIP_FIELD:
                delivery.packing_slip = self._coerce_int(value)
            elif key == "Route Sequence":
                delivery.route_sequence = self._coerce_int(value)

    def _apply_jobsite_fields(self, jobsite: Jobsite, fields: Dict[str, Any]) -> None:
        for key, value in (fields or {}).items():
            if key == "Job ID":
                jobsite.job_id = self._coerce_int(value) or jobsite.job_id
            elif key == settings.JOBSITES_NAME_FIELD:
                jobsite.name = self._first_str(value)
            elif key == settings.JOBSITES_ADDRESS_FIELD:
                jobsite.address = self._first_str(value) if value not in (None, "") else None
            elif key == settings.JOBSITES_LAT_FIELD:
                jobsite.latitude = self._coerce_float(value)
            elif key == settings.JOBSITES_LON_FIELD:
                jobsite.longitude = self._coerce_float(value)
            elif key == "Site Contact":
                jobsite.site_contact = self._first_str(value) if value not in (None, "") else None
            elif key == "Site Contact Number":
                jobsite.site_contact_number = self._first_str(value) if value not in (None, "") else None

    def _apply_user_fields(self, user: User, fields: Dict[str, Any]) -> None:
        for key, value in (fields or {}).items():
            if key == settings.USERS_EMAIL_FIELD:
                normalized = self._normalize_email(value)
                if normalized:
                    user.email = normalized
            elif key == settings.USERS_PASSWORD_FIELD:
                user.password_hash = self._first_str(value).strip() if value not in (None, "") else None
            elif key == settings.USERS_ROLE_FIELD:
                user.role = self._first_str(value) if value not in (None, "") else None
            elif key == settings.USERS_ACTIVE_FIELD:
                user.active = self._to_bool(value)
            elif key == settings.USERS_NAME_FIELD:
                if hasattr(user, "name"):
                    user.name = self._first_str(value) if value not in (None, "") else None
                elif hasattr(user, "full_name"):
                    user.full_name = self._first_str(value) if value not in (None, "") else None
            elif key == settings.USERS_SESSION_TOKEN_FIELD:
                if hasattr(user, "session_token"):
                    user.session_token = self._first_str(value) if value not in (None, "") else None
            elif key == settings.USERS_SESSION_EXPIRES_FIELD:
                if hasattr(user, "session_expires"):
                    user.session_expires = self._coerce_datetime(value)

    def _apply_plan_fields(self, plan: PublishedPlan, fields: Dict[str, Any]) -> None:
        for key, value in (fields or {}).items():
            if key == settings.PUBLISHED_PLANS_DATE_FIELD:
                plan.plan_date = self._coerce_date(value) or plan.plan_date
            elif key == settings.PUBLISHED_PLANS_TYPE_FIELD:
                plan.plan_type = self._normalize_plan_type(self._first_str(value), default="published")
            elif key == settings.PUBLISHED_PLANS_FLEET_FIELD:
                plan.fleet_json = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_ROUTES_FIELD:
                plan.routes_json = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_DELIVERIES_FIELD:
                plan.deliveries_json = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_META_FIELD:
                plan.meta_json = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD:
                plan.driver_profiles_json = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_STATUS_FIELD:
                plan.status = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD:
                plan.published_by = self._first_str(value) if value not in (None, "") else None
            elif key == settings.PUBLISHED_PLANS_UPDATED_AT_FIELD:
                plan.published_at = self._coerce_datetime(value)

    def _get_record_by_pk(self, session, table_name: str, record_id: str):
        if self._is_deliveries_table(table_name):
            try:
                pk = int(record_id)
            except Exception:
                return None
            return session.get(Delivery, pk)
        if self._is_jobsites_table(table_name):
            try:
                pk = int(record_id)
            except Exception:
                return None
            return session.get(Jobsite, pk)
        if self._is_users_table(table_name):
            raw = str(record_id or "").strip()
            if not raw:
                return None
            if raw.isdigit() and hasattr(User, "user_id"):
                rec = session.get(User, int(raw))
                if rec:
                    return rec
            normalized = self._normalize_email(raw)
            return session.execute(
                select(User).where(func.lower(func.trim(User.email)) == normalized)
            ).scalar_one_or_none()
        if self._is_published_plans_table(table_name):
            raw_id = str(record_id or "").strip()
            if "::" in raw_id:
                date_part, type_part = raw_id.split("::", 1)
            else:
                date_part, type_part = raw_id, "published"
            pk = self._coerce_date(date_part)
            if not pk:
                return None
            normalized_type = self._normalize_plan_type(type_part, default="published")
            stmt = select(PublishedPlan).where(
                PublishedPlan.plan_date == pk,
                PublishedPlan.plan_type == normalized_type,
            )
            rec = session.execute(stmt).scalar_one_or_none()
            if rec:
                return rec
            if normalized_type == "published":
                legacy_stmt = select(PublishedPlan).where(
                    PublishedPlan.plan_date == pk,
                    PublishedPlan.plan_type.is_(None),
                )
                return session.execute(legacy_stmt).scalar_one_or_none()
            return None
        return None

    @staticmethod
    def _parse_business_weekdays() -> set[int]:
        raw = (settings.BUSINESS_WEEKDAYS or "").strip()
        if not raw:
            return {0, 1, 2, 3, 4}
        parts = re.split(r"[,\s]+", raw)
        days = set()
        for part in parts:
            if not part:
                continue
            try:
                days.add(int(part))
            except Exception:
                continue
        return days or {0, 1, 2, 3, 4}

    @staticmethod
    def _parse_holidays() -> set[date]:
        raw = (settings.HOLIDAY_DATES or "").strip()
        if not raw:
            return set()
        holidays = set()
        for token in re.split(r"[,\s]+", raw):
            if not token:
                continue
            try:
                holidays.add(date.fromisoformat(token))
            except Exception:
                continue
        return holidays

    @classmethod
    def _next_business_day(cls, start_date: date) -> date:
        weekdays = cls._parse_business_weekdays()
        holidays = cls._parse_holidays()
        cursor = start_date
        while cursor.weekday() not in weekdays or cursor in holidays:
            cursor += timedelta(days=1)
        return cursor

    @staticmethod
    def next_business_date(from_date: date | None = None) -> date:
        tz = ZoneInfo(settings.BUSINESS_TZ)
        base = from_date or datetime.now(tz).date()
        start = base + timedelta(days=1)
        return DatabaseService._next_business_day(start)

    @staticmethod
    def next_business_date_iso(from_date: date | None = None) -> str:
        return DatabaseService.next_business_date(from_date).isoformat()

    def _resolve_target_date(self, deliveries_records: list[Dict[str, Any]] | None = None) -> date:
        tz = ZoneInfo(settings.BUSINESS_TZ)
        base_date = datetime.now(tz).date()
        start_date = base_date + timedelta(days=1)
        strategy = (settings.NEXT_DELIVERY_STRATEGY or "business_day").strip().lower()
        if strategy == "next_scheduled":
            lookahead = max(1, int(settings.NEXT_DELIVERY_LOOKAHEAD_DAYS or 30))
            max_date = start_date + timedelta(days=lookahead)
            with SessionLocal() as session:
                candidate = session.execute(
                    select(func.min(Delivery.scheduled_date)).where(
                        Delivery.scheduled_date.is_not(None),
                        Delivery.scheduled_date >= start_date,
                        Delivery.scheduled_date <= max_date,
                    )
                ).scalar_one()
            if candidate:
                return candidate
        return self._next_business_day(start_date)

    def get_next_delivery_date(self) -> str:
        target = self._resolve_target_date()
        return target.isoformat()

    def bulk_update_deliveries_by_business_id(self, updates: list[Dict[str, Any]]) -> Dict[str, Any]:
        updated_ids: list[str] = []
        errors: list[Dict[str, Any]] = []

        with SessionLocal() as session:
            for item in updates:
                delivery_id_raw = str(item.get("delivery_id", "")).strip()
                if not delivery_id_raw:
                    errors.append({"delivery_id": delivery_id_raw, "error": "Missing delivery_id."})
                    continue
                try:
                    delivery_id = int(delivery_id_raw)
                except Exception:
                    errors.append({"delivery_id": delivery_id_raw, "error": "Invalid delivery_id."})
                    continue
                delivery = session.get(Delivery, delivery_id)
                if not delivery:
                    errors.append({"delivery_id": delivery_id_raw, "error": "Delivery not found."})
                    continue

                if "med_skid" in item:
                    delivery.med_skid = self._coerce_int(item.get("med_skid"))
                if "ps_delivered" in item:
                    delivery.ps_delivered = self._to_bool(item.get("ps_delivered"))
                if "skid" in item:
                    delivery.med_skid = self._coerce_int(item.get("skid"))
                if "small_skid" in item:
                    delivery.small_skid = self._coerce_int(item.get("small_skid"))
                if "large_skid" in item:
                    delivery.large_skid = self._coerce_int(item.get("large_skid"))
                if "pickup" in item:
                    delivery.pickup = self._to_bool(item.get("pickup"))
                if "pickup_vehicle" in item:
                    delivery.pickup_vehicle = self._normalize_pickup_vehicle(item.get("pickup_vehicle"))
                if "requires_empty_truck" in item:
                    delivery.requires_empty_truck = self._to_bool(item.get("requires_empty_truck"))
                if "door" in item:
                    delivery.door = self._coerce_int(item.get("door"))
                if "frame" in item:
                    delivery.frame = self._coerce_int(item.get("frame"))
                if "exp_frame" in item:
                    delivery.exp_frame = self._coerce_int(item.get("exp_frame"))
                if "box" in item:
                    delivery.box = self._coerce_int(item.get("box"))
                if "window_start" in item:
                    value = item.get("window_start")
                    delivery.window_start = self._first_str(value) if value not in (None, "") else None
                if "window_end" in item:
                    value = item.get("window_end")
                    delivery.window_end = self._first_str(value) if value not in (None, "") else None

                if hasattr(delivery, "updated_at"):
                    delivery.updated_at = datetime.now(ZoneInfo("UTC"))
                updated_ids.append(str(delivery.delivery_id))
            session.commit()
        if updated_ids:
            self._invalidate_cache("Deliveries")

        return {
            "updated": len(updated_ids),
            "errors": errors,
        }
        
    def list_records(self, table_name: str, page_size: int = 100, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if not force_refresh:
            cached = self._get_cached_table(table_name)
            if cached is not None:
                return cached
        with SessionLocal() as session:
            if self._is_jobsites_table(table_name):
                records = session.execute(select(Jobsite)).scalars().all()
                data = [self._jobsite_record(r) for r in records]
                self._set_cached_table(table_name, data)
                return data
            if self._is_deliveries_table(table_name):
                records = session.execute(select(Delivery)).scalars().all()
                data = [self._delivery_record(r) for r in records]
                self._set_cached_table(table_name, data)
                return data
            if self._is_users_table(table_name):
                if self._table_exists("users", session.bind):
                    records = session.execute(select(User)).scalars().all()
                    data = [self._user_record(r) for r in records]
                elif self._table_exists("Users", session.bind):
                    rows = session.execute(
                        text(
                            f"""
                            {self._users_select_sql()}
                            ORDER BY "Email"
                            """
                        )
                    ).mappings().all()
                    data = [self._legacy_user_record(dict(row)) for row in rows]
                else:
                    data = []
                self._set_cached_table(table_name, data)
                return data
            if self._is_published_plans_table(table_name):
                data = self.list_published_plans(force_refresh=force_refresh, plan_type="published")
                self._set_cached_table(table_name, data)
                return data
        raise ValueError(f"Unsupported table: {table_name}")

    def get_record(self, table_name: str, record_id: str) -> Dict[str, Any]:
        with SessionLocal() as session:
            rec = self._get_record_by_pk(session, table_name, record_id)
            if not rec:
                raise ValueError("Record not found.")
            if self._is_jobsites_table(table_name):
                return self._jobsite_record(rec)
            if self._is_deliveries_table(table_name):
                return self._delivery_record(rec)
            if self._is_users_table(table_name):
                return self._user_record(rec)
            if self._is_published_plans_table(table_name):
                return self._plan_record(rec)
        raise ValueError(f"Unsupported table: {table_name}")

    def create_record(self, table_name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with SessionLocal() as session:
            if self._is_jobsites_table(table_name):
                jobsite = Jobsite(name="")
                self._apply_jobsite_fields(jobsite, fields)
                session.add(jobsite)
                session.commit()
                session.refresh(jobsite)
                self._invalidate_cache("Jobsites")
                return self._jobsite_record(jobsite)
            if self._is_deliveries_table(table_name):
                delivery = Delivery()
                self._apply_delivery_fields(delivery, fields)
                session.add(delivery)
                session.commit()
                session.refresh(delivery)
                self._invalidate_cache("Deliveries")
                return self._delivery_record(delivery)
            if self._is_users_table(table_name):
                user = User(email="")
                self._apply_user_fields(user, fields)
                session.add(user)
                session.commit()
                session.refresh(user)
                self._invalidate_cache(settings.USERS_TABLE)
                return self._user_record(user)
            if self._is_published_plans_table(table_name):
                plan_date = self._coerce_date(fields.get(settings.PUBLISHED_PLANS_DATE_FIELD)) or date.today()
                plan_type = self._normalize_plan_type(fields.get(settings.PUBLISHED_PLANS_TYPE_FIELD), default="published")
                plan = PublishedPlan(plan_date=plan_date, plan_type=plan_type)
                self._apply_plan_fields(plan, fields)
                session.add(plan)
                session.commit()
                session.refresh(plan)
                self._invalidate_cache(settings.PUBLISHED_PLANS_TABLE)
                return self._plan_record(plan)
        raise ValueError(f"Unsupported table: {table_name}")

    def update_record(self, table_name: str, record_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with SessionLocal() as session:
            rec = self._get_record_by_pk(session, table_name, record_id)
            if not rec:
                raise ValueError("Record not found.")
            if self._is_jobsites_table(table_name):
                self._apply_jobsite_fields(rec, fields)
                if hasattr(rec, "updated_at"):
                    rec.updated_at = datetime.now(ZoneInfo("UTC"))
                session.commit()
                session.refresh(rec)
                self._invalidate_cache("Jobsites")
                return self._jobsite_record(rec)
            if self._is_deliveries_table(table_name):
                self._apply_delivery_fields(rec, fields)
                if hasattr(rec, "updated_at"):
                    rec.updated_at = datetime.now(ZoneInfo("UTC"))
                session.commit()
                session.refresh(rec)
                self._invalidate_cache("Deliveries")
                return self._delivery_record(rec)
            if self._is_users_table(table_name):
                self._apply_user_fields(rec, fields)
                if hasattr(rec, "updated_at"):
                    rec.updated_at = datetime.now(ZoneInfo("UTC"))
                session.commit()
                session.refresh(rec)
                self._invalidate_cache(settings.USERS_TABLE)
                return self._user_record(rec)
            if self._is_published_plans_table(table_name):
                self._apply_plan_fields(rec, fields)
                if hasattr(rec, "updated_at"):
                    rec.updated_at = datetime.now(ZoneInfo("UTC"))
                session.commit()
                session.refresh(rec)
                self._invalidate_cache(settings.PUBLISHED_PLANS_TABLE)
                return self._plan_record(rec)
        raise ValueError(f"Unsupported table: {table_name}")

    def delete_record(self, table_name: str, record_id: str) -> Dict[str, Any]:
        with SessionLocal() as session:
            rec = self._get_record_by_pk(session, table_name, record_id)
            if not rec:
                raise ValueError("Record not found.")
            if self._is_jobsites_table(table_name):
                payload = self._jobsite_record(rec)
            elif self._is_deliveries_table(table_name):
                payload = self._delivery_record(rec)
            elif self._is_users_table(table_name):
                payload = self._user_record(rec)
            elif self._is_published_plans_table(table_name):
                payload = self._plan_record(rec)
            else:
                raise ValueError(f"Unsupported table: {table_name}")
            session.delete(rec)
            session.commit()
            self._invalidate_cache(table_name)
            return payload

    def list_jobsites_min(self, force_refresh: bool = False) -> List[Dict[str, str]]:
        """
        Returns jobsites for dropdown: [{id, name, address}]
        Uses Airtable fields: name/address.
        """
        records = self.list_records("Jobsites", force_refresh=force_refresh)
        out = []
        for rec in records:
            f = rec.get("fields", {})
            out.append(
                {
                    "id": rec.get("id", ""),
                    "name": self._first_str(f.get(settings.JOBSITES_NAME_FIELD)),
                    "address": self._first_str(f.get(settings.JOBSITES_ADDRESS_FIELD)),
                    "site_contact": self._first_str(f.get("Site Contact")),
                    "site_contact_number": self._first_str(f.get("Site Contact Number")),
                    "latitude": self._coerce_float(f.get(settings.JOBSITES_LAT_FIELD)),
                    "longitude": self._coerce_float(f.get(settings.JOBSITES_LON_FIELD)),
                }
            )
        out.sort(key=lambda x: (x["name"] or "").lower())
        return out

    def find_jobsite_by_address(self, address: str) -> Dict[str, Any] | None:
        if not address:
            return None
        needle = self._normalize_address(address)
        if not needle:
            return None
        records = self.list_records("Jobsites")
        for rec in records:
            fields = rec.get("fields", {})
            addr = self._first_str(fields.get(settings.JOBSITES_ADDRESS_FIELD))
            if self._normalize_address(addr) == needle:
                return rec
        return None

    @staticmethod
    def _json_dumps(value) -> str:
        try:
            return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        except Exception:
            return json.dumps(str(value))

    def _json_load(self, value, default):
        if not value:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

    def list_deliveries_min(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Returns deliveries with jobsite name/address for manager list.
        """
        deliveries_records = self.list_records("Deliveries", force_refresh=force_refresh)
        jobsites_records = self.list_records("Jobsites", force_refresh=force_refresh)

        jobsite_by_id: Dict[str, Dict[str, Any]] = {
            r.get("id"): r.get("fields", {}) for r in jobsites_records
        }

        link_field = settings.DELIVERIES_JOB_LINK_FIELD
        out: List[Dict[str, Any]] = []

        for rec in deliveries_records:
            fields = rec.get("fields", {})
            jobsite_ids = fields.get(link_field) or []
            jobsite_id = jobsite_ids[0] if isinstance(jobsite_ids, list) and jobsite_ids else None
            jobsite_fields = jobsite_by_id.get(jobsite_id, {}) if jobsite_id else {}

            job_name = (
                self._first_str(jobsite_fields.get(settings.JOBSITES_NAME_FIELD))
                or self._first_str(fields.get("Job Name"))
            )
            job_address = (
                self._first_str(jobsite_fields.get(settings.JOBSITES_ADDRESS_FIELD))
                or self._first_str(fields.get("Job Address"))
            )
            sales_order = self._first_str(fields.get(settings.DELIVERIES_SALES_ORDER_FIELD))
            packing_slip = self._first_str(fields.get(settings.DELIVERIES_PACKING_SLIP_FIELD))
            driver = self._first_str(fields.get(settings.DELIVERIES_DRIVER_FIELD))
            status = self._first_str(fields.get(settings.DELIVERIES_STATUS_FIELD) or fields.get("Status"))
            status_updated_at = self._first_str(fields.get(settings.DELIVERIES_STATUS_UPDATED_FIELD))
            arrived_at = self._first_str(fields.get(settings.DELIVERIES_ARRIVED_AT_FIELD))
            delivered_at = self._first_str(fields.get(settings.DELIVERIES_DELIVERED_AT_FIELD))
            driver_lat = self._coerce_float(fields.get(settings.DELIVERIES_DRIVER_LAT_FIELD))
            driver_lon = self._coerce_float(fields.get(settings.DELIVERIES_DRIVER_LON_FIELD))
            driver_notes = self._first_str(fields.get(settings.DELIVERIES_DRIVER_NOTES_FIELD))
            if not status and driver_notes.lower().startswith("[status:"):
                closing = driver_notes.find("]")
                if closing > 8:
                    status = driver_notes[8:closing].strip().lower().replace("-", "_").replace(" ", "_")
            ps_delivered = self._to_bool(fields.get(settings.DELIVERIES_PS_DELIVERED_FIELD))
            pickup_flag = self._to_bool(fields.get("Pickup"))
            pickup_vehicle = self._normalize_pickup_vehicle(fields.get(settings.DELIVERIES_PICKUP_VEHICLE_FIELD))
            if pickup_flag and not pickup_vehicle:
                pickup_vehicle = "both"
            if not pickup_flag:
                pickup_vehicle = ""

            out.append({
                "record_id": rec.get("id"),
                "delivery_id": self._first_str(fields.get("Delivery ID") or rec.get("id")),
                "jobsite_id": jobsite_id,
                "job_name": job_name,
                "job_address": job_address,
                "sales_order": sales_order,
                "packing_slip": packing_slip,
                "driver": driver,
                "status": status,
                "status_updated_at": status_updated_at,
                "arrived_at": arrived_at,
                "delivered_at": delivered_at,
                "route_sequence": self._coerce_int(fields.get("Route Sequence")),
                "driver_lat": driver_lat,
                "driver_lon": driver_lon,
                "driver_notes": driver_notes,
                "ps_delivered": ps_delivered,
                "scheduled_date": self._first_str(fields.get("Scheduled Date")),
                "med_skid": self._first_str(fields.get("Med Skid") or fields.get("Skid")),
                "small_skid": self._first_str(fields.get("Small Skid")),
                "large_skid": self._first_str(fields.get("Large Skid")),
                "frame": self._first_str(fields.get("Frame")),
                "exp_frame": self._first_str(fields.get("Exp Frame")),
                "door": self._first_str(fields.get("Door")),
                "box": self._first_str(fields.get("Box")),
                "pickup": pickup_flag,
                "pickup_vehicle": pickup_vehicle,
                "requires_empty_truck": self._to_bool(fields.get("Requires Empty Truck")),
                "window_start": self._first_str(fields.get("Window Start")),
                "window_end": self._first_str(fields.get("Window End")),
                "updated_at": self._first_str(fields.get("Updated At")),
            })

        out.sort(key=lambda x: (x["scheduled_date"] or "", x["delivery_id"] or ""))
        return out

    @staticmethod
    def _route_driver_name_from_payload(route: Dict[str, Any]) -> str:
        profile = route.get("driver_profile")
        if isinstance(profile, dict):
            for key in ("id", "email", "name"):
                value = str(profile.get(key) or "").strip()
                if value:
                    return value
        elif isinstance(profile, str):
            value = profile.strip()
            if value:
                return value
        return str(route.get("driver") or "").strip()

    def _resolve_driver_user_id(
        self,
        session,
        driver_ref: Any,
        users_cols: set[str] | None = None,
    ) -> int | None:
        raw = self._first_str(driver_ref)
        if not raw:
            return None

        try:
            return int(float(raw))
        except (TypeError, ValueError):
            pass

        if not self._table_exists("users", session.bind):
            return None

        cols = users_cols if users_cols is not None else self._users_column_names(session.bind)
        if "user_id" not in cols:
            return None

        normalized = raw.strip().lower()

        if "email" in cols:
            row = session.execute(
                text(
                    """
                    SELECT user_id
                    FROM users
                    WHERE lower(trim(email)) = :value
                    LIMIT 1
                    """
                ),
                {"value": normalized},
            ).mappings().first()
            if row and row.get("user_id") is not None:
                try:
                    return int(row.get("user_id"))
                except (TypeError, ValueError):
                    pass

        name_predicates: List[str] = []
        if "name" in cols:
            name_predicates.append("lower(trim(name)) = :value")
        if "full_name" in cols:
            name_predicates.append("lower(trim(full_name)) = :value")
        if name_predicates:
            row = session.execute(
                text(
                    f"""
                    SELECT user_id
                    FROM users
                    WHERE {' OR '.join(name_predicates)}
                    ORDER BY user_id
                    LIMIT 1
                    """
                ),
                {"value": normalized},
            ).mappings().first()
            if row and row.get("user_id") is not None:
                try:
                    return int(row.get("user_id"))
                except (TypeError, ValueError):
                    pass

        return None

    @staticmethod
    def _route_delivery_assignments(route: Dict[str, Any]) -> List[tuple[str, int]]:
        assigned: List[tuple[str, int]] = []
        stops = route.get("stops")
        if isinstance(stops, list):
            for stop_index, stop in enumerate(stops, start=1):
                group = stop if isinstance(stop, list) else [stop]
                for item in group:
                    if isinstance(item, dict):
                        if bool(item.get("custom_stop")):
                            continue
                        if str(item.get("type") or "").strip().lower() != "delivery":
                            continue
                        delivery_id = str(item.get("id") or item.get("delivery_id") or "").strip()
                        if delivery_id:
                            assigned.append((delivery_id, stop_index))
                    elif isinstance(item, (str, int)):
                        token = str(item).strip()
                        if not token or token.lower() in {"depot", "depot - reload"}:
                            continue
                        base_id = token.split("_Part", 1)[0].strip()
                        if base_id:
                            assigned.append((base_id, stop_index))
        elif isinstance(route.get("jobs"), list):
            for stop_index, stop in enumerate(route.get("jobs") or [], start=1):
                if not isinstance(stop, list):
                    continue
                for token in stop:
                    raw = str(token or "").strip()
                    if not raw or raw.lower() in {"depot", "depot - reload"}:
                        continue
                    base_id = raw.split("_Part", 1)[0].strip()
                    if base_id:
                        assigned.append((base_id, stop_index))
        return assigned

    def build_delivery_plan_payload(self, plan_date: str | date, force_refresh: bool = False) -> Dict[str, Any]:
        target_date = self._coerce_date(plan_date)
        if not target_date:
            raise ValueError("Invalid plan_date.")

        target_iso = target_date.isoformat()
        rows = [row for row in self.list_deliveries_min(force_refresh=force_refresh) if row.get("scheduled_date") == target_iso]
        if not rows:
            return {
                "plan_date": target_iso,
                "routes": [],
                "deliveries": [],
                "meta": {
                    "source": "deliveries",
                    "depot_lat": settings.DEPOT_LAT,
                    "depot_lon": settings.DEPOT_LON,
                    "plan_date": target_iso,
                },
                "fleet": {},
                "driver_profiles": [],
                "status": "Live",
                "updated_at": None,
                "updated_by": None,
            }

        deliveries: List[Dict[str, Any]] = []
        route_groups: Dict[str, List[Dict[str, Any]]] = {}
        latest_update: datetime | None = None

        for row in rows:
            delivery_id = str(row.get("delivery_id") or "").strip()
            if not delivery_id:
                continue
            med_skid = self._coerce_int(row.get("med_skid")) or 0
            small_skid = self._coerce_int(row.get("small_skid")) or 0
            large_skid = self._coerce_int(row.get("large_skid")) or 0
            door = self._coerce_int(row.get("door")) or 0
            frame = self._coerce_int(row.get("frame")) or 0
            exp_frame = self._coerce_int(row.get("exp_frame")) or 0
            pickup_flag = self._to_bool(row.get("pickup"))
            pickup_vehicle = self._normalize_pickup_vehicle(row.get("pickup_vehicle"))
            if pickup_flag and not pickup_vehicle:
                pickup_vehicle = "both"
            if not pickup_flag:
                pickup_vehicle = ""

            delivery = {
                "id": delivery_id,
                "delivery_id": delivery_id,
                "jobsite_id": self._first_str(row.get("jobsite_id")),
                "job_name": self._first_str(row.get("job_name")),
                "job_address": self._first_str(row.get("job_address")),
                "sales_order": self._first_str(row.get("sales_order")),
                "packing_slip": self._first_str(row.get("packing_slip")),
                "driver": self._first_str(row.get("driver")),
                "status": self._first_str(row.get("status")),
                "status_updated_at": self._first_str(row.get("status_updated_at")),
                "arrived_at": self._first_str(row.get("arrived_at")),
                "delivered_at": self._first_str(row.get("delivered_at")),
                "ps_delivered": self._to_bool(row.get("ps_delivered")),
                "route_sequence": self._coerce_int(row.get("route_sequence")),
                "scheduled_date": target_iso,
                "med_skid": med_skid,
                "small_skid": small_skid,
                "large_skid": large_skid,
                "door": door,
                "frame": frame,
                "exp_frame": exp_frame,
                "box": self._coerce_int(row.get("box")) or 0,
                "pickup": pickup_flag,
                "pickup_vehicle": pickup_vehicle,
                "requires_empty_truck": self._to_bool(row.get("requires_empty_truck")),
                "window_start": self._first_str(row.get("window_start")),
                "window_end": self._first_str(row.get("window_end")),
                "capacity_units": (
                    med_skid * int(getattr(settings, "MED_SKID_UNITS", getattr(settings, "LONG_SKID_UNITS", 20)))
                    + large_skid * int(getattr(settings, "LARGE_SKID_UNITS", 25))
                    + small_skid * int(getattr(settings, "SMALL_SKID_UNITS", 10))
                    + (frame + exp_frame) * int(getattr(settings, "FRAME_UNITS", 1))
                ),
            }
            delivery["requires_truck"] = bool(
                delivery["door"] > 0
                or delivery["frame"] > 0
                or delivery["exp_frame"] > 0
                or delivery["med_skid"] > 0
                or delivery["small_skid"] > 0
                or delivery["large_skid"] > 0
                or (pickup_flag and (pickup_vehicle == "truck" or delivery["requires_empty_truck"]))
            )
            deliveries.append(delivery)

            driver_name = self._first_str(row.get("driver")) or "Unassigned"
            route_groups.setdefault(driver_name, []).append(delivery)

            updated_at = self._coerce_datetime(delivery.get("status_updated_at"))
            if updated_at and (latest_update is None or updated_at > latest_update):
                latest_update = updated_at
            row_updated_at = self._coerce_datetime(row.get("updated_at"))
            if row_updated_at and (latest_update is None or row_updated_at > latest_update):
                latest_update = row_updated_at

        routes: List[Dict[str, Any]] = []
        driver_profiles: List[Dict[str, Any]] = []
        sorted_driver_names = sorted(route_groups.keys(), key=lambda name: name.lower())
        for idx, driver_name in enumerate(sorted_driver_names, start=1):
            grouped = route_groups.get(driver_name) or []
            grouped.sort(
                key=lambda item: (
                    item.get("route_sequence") if item.get("route_sequence") is not None else 2147483647,
                    str(item.get("delivery_id") or ""),
                )
            )
            stops: List[List[Dict[str, Any]]] = []
            stop_times: List[Dict[str, Any]] = []
            for item in grouped:
                stops.append(
                    [
                        {
                            "type": "delivery",
                            "id": item.get("delivery_id"),
                            "job_name": item.get("job_name"),
                            "med_skid": item.get("med_skid"),
                            "small_skid": item.get("small_skid"),
                            "large_skid": item.get("large_skid"),
                            "door": item.get("door"),
                            "frame": item.get("frame"),
                            "exp_frame": item.get("exp_frame"),
                            "box": item.get("box"),
                        }
                    ]
                )
                stop_times.append(
                    {
                        "arrival_hhmm": self._format_time_value(item.get("window_start")),
                        "departure_hhmm": self._format_time_value(item.get("window_end")),
                    }
                )
            route: Dict[str, Any] = {
                "vehicle_id": idx,
                "vehicle_type": "truck",
                "driver": "" if driver_name == "Unassigned" else driver_name,
                "stops": stops,
                "stop_times": stop_times,
                "total_distance_m": 0,
                "total_time_s": 0,
            }
            if driver_name != "Unassigned":
                route["driver_profile"] = {"name": driver_name}
                driver_profiles.append({"name": driver_name})
            routes.append(route)

        return {
            "plan_date": target_iso,
            "routes": routes,
            "deliveries": deliveries,
            "meta": {
                "source": "deliveries",
                "depot_lat": settings.DEPOT_LAT,
                "depot_lon": settings.DEPOT_LON,
                "plan_date": target_iso,
                "driver_assignments": {
                    str(route.get("vehicle_id")): route.get("driver")
                    for route in routes
                    if str(route.get("driver") or "").strip()
                },
            },
            "fleet": {
                "trucks": len(routes),
                "vans": 0,
            },
            "driver_profiles": driver_profiles,
            "status": "Live",
            "updated_at": latest_update.isoformat() if latest_update else None,
            "updated_by": None,
        }

    def apply_route_assignments(
        self,
        plan_date: str | date,
        routes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        target_date = self._coerce_date(plan_date)
        if not target_date:
            raise ValueError("Invalid plan_date.")

        assignments: Dict[int, Dict[str, Any]] = {}
        for route in routes or []:
            if not isinstance(route, dict):
                continue
            driver_name = self._route_driver_name_from_payload(route)
            route_assignments = self._route_delivery_assignments(route)
            seen: set[int] = set()
            for raw_id, stop_seq in route_assignments:
                try:
                    delivery_id = int(str(raw_id).strip())
                except Exception:
                    continue
                if delivery_id in seen:
                    continue
                seen.add(delivery_id)
                assignments[delivery_id] = {
                    "driver_name": driver_name.strip() or None,
                    "route_sequence": stop_seq,
                }

        touched = 0
        assigned = 0
        unresolved_driver_refs: set[str] = set()
        with SessionLocal() as session:
            deliveries = session.execute(
                select(Delivery).where(Delivery.scheduled_date == target_date)
            ).scalars().all()
            users_cols = self._users_column_names(session.bind) if self._table_exists("users", session.bind) else set()
            driver_id_cache: Dict[str, int | None] = {}
            now_utc = datetime.now(ZoneInfo("UTC"))
            for delivery in deliveries:
                touched += 1
                payload = assignments.get(int(delivery.delivery_id))
                if payload:
                    driver_ref = self._first_str(payload.get("driver_name"))
                    driver_id: int | None = None
                    if driver_ref:
                        cache_key = driver_ref.lower()
                        if cache_key not in driver_id_cache:
                            driver_id_cache[cache_key] = self._resolve_driver_user_id(
                                session,
                                driver_ref,
                                users_cols=users_cols,
                            )
                        driver_id = driver_id_cache.get(cache_key)

                    if hasattr(delivery, "assigned_driver_id"):
                        delivery.assigned_driver_id = driver_id
                        if driver_ref and driver_id is None:
                            unresolved_driver_refs.add(driver_ref)
                        if driver_id is not None:
                            assigned += 1
                    else:
                        delivery.drivers = driver_ref or None
                        if driver_ref:
                            assigned += 1

                    delivery.route_sequence = payload.get("route_sequence")
                else:
                    if hasattr(delivery, "assigned_driver_id"):
                        delivery.assigned_driver_id = None
                    else:
                        delivery.drivers = None
                    delivery.route_sequence = None
                if hasattr(delivery, "updated_at"):
                    delivery.updated_at = now_utc
            session.commit()

        self._invalidate_cache("Deliveries")
        return {
            "date": target_date.isoformat(),
            "touched": touched,
            "assigned": assigned,
            "unassigned": max(0, touched - assigned),
            "unresolved_driver_refs": sorted(unresolved_driver_refs),
        }

    @staticmethod
    def _live_route_key(driver_name: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", (driver_name or "").strip().lower())
        normalized = normalized.strip("-")
        return f"driver:{normalized or 'unassigned'}"

    @staticmethod
    def _iso_datetime(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _normalize_progress_from_delivery(delivery: Dict[str, Any]) -> tuple[str, str, str]:
        status_raw = str(delivery.get("status") or "").strip().lower().replace("-", "_").replace(" ", "_")
        delivered_at = str(delivery.get("delivered_at") or "").strip()
        status_updated = str(delivery.get("status_updated_at") or "").strip()
        arrived_at = str(delivery.get("arrived_at") or "").strip()
        ps_delivered = bool(delivery.get("ps_delivered"))

        if status_raw == "failed":
            return "failed", "Failed", status_updated or arrived_at
        if status_raw == "delivered" or delivered_at or ps_delivered:
            return "done", "Delivered", delivered_at or status_updated or arrived_at
        if status_raw == "arrived":
            return "current", "Arrived", status_updated or arrived_at
        if status_raw in {"en_route", "in_progress"}:
            return "current", "En Route", status_updated or arrived_at
        return "upcoming", "Upcoming", ""

    def list_live_route_rows(self, live_date: str | date | None = None, force_refresh: bool = False) -> List[Dict[str, Any]]:
        target_date = self._coerce_date(live_date)
        if not target_date:
            target_date = datetime.now(ZoneInfo(settings.BUSINESS_TZ)).date()

        with SessionLocal() as session:
            if self._supports_live_views(session.bind):
                rows = session.execute(
                    text(
                        """
                        SELECT
                            live_date,
                            route_key,
                            driver_name,
                            delivery_id,
                            jobsite_id,
                            job_name,
                            job_address,
                            sales_order,
                            packing_slip,
                            route_sequence,
                            status_code,
                            progress_state,
                            status_label,
                            status_time,
                            status_updated_at,
                            arrived_at,
                            delivered_at,
                            driver_latitude,
                            driver_longitude,
                            med_skid,
                            small_skid,
                            large_skid,
                            door,
                            frame,
                            exp_frame,
                            box,
                            pickup,
                            pickup_vehicle,
                            requires_empty_truck,
                            window_start,
                            window_end,
                            delivery_notes
                        FROM public.v_route_live
                        WHERE live_date = :live_date
                        ORDER BY
                            lower(driver_name),
                            COALESCE(route_sequence, 2147483647),
                            delivery_id
                        """
                    ),
                    {"live_date": target_date},
                ).mappings().all()
                output: List[Dict[str, Any]] = []
                for row in rows:
                    output.append(
                        {
                            "live_date": str(row.get("live_date") or ""),
                            "route_key": self._first_str(row.get("route_key")),
                            "driver_name": self._first_str(row.get("driver_name")),
                            "delivery_id": self._first_str(row.get("delivery_id")),
                            "jobsite_id": self._first_str(row.get("jobsite_id")),
                            "job_name": self._first_str(row.get("job_name")),
                            "job_address": self._first_str(row.get("job_address")),
                            "sales_order": self._first_str(row.get("sales_order")),
                            "packing_slip": self._first_str(row.get("packing_slip")),
                            "route_sequence": row.get("route_sequence"),
                            "status_code": self._first_str(row.get("status_code")),
                            "progress_state": self._first_str(row.get("progress_state")) or "upcoming",
                            "status_label": self._first_str(row.get("status_label")) or "Upcoming",
                            "status_time": self._iso_datetime(row.get("status_time")),
                            "status_updated_at": self._iso_datetime(row.get("status_updated_at")),
                            "arrived_at": self._iso_datetime(row.get("arrived_at")),
                            "delivered_at": self._iso_datetime(row.get("delivered_at")),
                            "driver_latitude": row.get("driver_latitude"),
                            "driver_longitude": row.get("driver_longitude"),
                            "med_skid": int(row.get("med_skid") or 0),
                            "small_skid": int(row.get("small_skid") or 0),
                            "large_skid": int(row.get("large_skid") or 0),
                            "door": int(row.get("door") or 0),
                            "frame": int(row.get("frame") or 0),
                            "exp_frame": int(row.get("exp_frame") or 0),
                            "box": int(row.get("box") or 0),
                            "pickup": bool(row.get("pickup")),
                            "pickup_vehicle": self._first_str(row.get("pickup_vehicle")),
                            "requires_empty_truck": bool(row.get("requires_empty_truck")),
                            "window_start": self._first_str(row.get("window_start")),
                            "window_end": self._first_str(row.get("window_end")),
                            "delivery_notes": self._first_str(row.get("delivery_notes")),
                        }
                    )
                return output

        # Fallback path for deployments where the live SQL views are not yet migrated.
        target_iso = target_date.isoformat()
        deliveries = self.list_deliveries_min(force_refresh=force_refresh)
        output: List[Dict[str, Any]] = []
        for item in deliveries:
            scheduled = self._first_str(item.get("scheduled_date"))
            if scheduled != target_iso:
                continue
            driver_name = self._first_str(item.get("driver")) or "Unassigned"
            progress_state, status_label, status_time = self._normalize_progress_from_delivery(item)
            output.append(
                {
                    "live_date": target_iso,
                    "route_key": self._live_route_key(driver_name),
                    "driver_name": driver_name,
                    "delivery_id": self._first_str(item.get("delivery_id")),
                    "jobsite_id": self._first_str(item.get("jobsite_id")),
                    "job_name": self._first_str(item.get("job_name")) or f"Delivery {self._first_str(item.get('delivery_id'))}",
                    "job_address": self._first_str(item.get("job_address")),
                    "sales_order": self._first_str(item.get("sales_order")),
                    "packing_slip": self._first_str(item.get("packing_slip")),
                    "route_sequence": self._coerce_int(item.get("route_sequence")),
                    "status_code": self._first_str(item.get("status")),
                    "progress_state": progress_state,
                    "status_label": status_label,
                    "status_time": status_time,
                    "status_updated_at": self._first_str(item.get("status_updated_at")),
                    "arrived_at": self._first_str(item.get("arrived_at")),
                    "delivered_at": self._first_str(item.get("delivered_at")),
                    "driver_latitude": item.get("driver_lat"),
                    "driver_longitude": item.get("driver_lon"),
                    "med_skid": self._coerce_int(item.get("med_skid")) or 0,
                    "small_skid": self._coerce_int(item.get("small_skid")) or 0,
                    "large_skid": self._coerce_int(item.get("large_skid")) or 0,
                    "door": self._coerce_int(item.get("door")) or 0,
                    "frame": self._coerce_int(item.get("frame")) or 0,
                    "exp_frame": self._coerce_int(item.get("exp_frame")) or 0,
                    "box": self._coerce_int(item.get("box")) or 0,
                    "pickup": self._to_bool(item.get("pickup")),
                    "pickup_vehicle": self._first_str(item.get("pickup_vehicle")),
                    "requires_empty_truck": self._to_bool(item.get("requires_empty_truck")),
                    "window_start": self._first_str(item.get("window_start")),
                    "window_end": self._first_str(item.get("window_end")),
                    "delivery_notes": self._first_str(item.get("driver_notes")),
                }
            )

        output.sort(
            key=lambda row: (
                (row.get("driver_name") or "").lower(),
                row.get("route_sequence") if row.get("route_sequence") is not None else 2147483647,
                row.get("delivery_id") or "",
            )
        )
        return output

    def list_live_driver_progress(self, live_date: str | date | None = None, force_refresh: bool = False) -> List[Dict[str, Any]]:
        target_date = self._coerce_date(live_date)
        if not target_date:
            target_date = datetime.now(ZoneInfo(settings.BUSINESS_TZ)).date()

        with SessionLocal() as session:
            if self._supports_live_views(session.bind):
                rows = session.execute(
                    text(
                        """
                        SELECT
                            live_date,
                            route_key,
                            driver_name,
                            total_stops,
                            done_stops,
                            current_stops,
                            upcoming_stops,
                            failed_stops,
                            last_completed_at,
                            last_status_update_at
                        FROM public.v_driver_progress_live
                        WHERE live_date = :live_date
                        ORDER BY lower(driver_name), route_key
                        """
                    ),
                    {"live_date": target_date},
                ).mappings().all()
                output: List[Dict[str, Any]] = []
                for row in rows:
                    output.append(
                        {
                            "live_date": str(row.get("live_date") or ""),
                            "route_key": self._first_str(row.get("route_key")),
                            "driver_name": self._first_str(row.get("driver_name")),
                            "total_stops": int(row.get("total_stops") or 0),
                            "done_stops": int(row.get("done_stops") or 0),
                            "current_stops": int(row.get("current_stops") or 0),
                            "upcoming_stops": int(row.get("upcoming_stops") or 0),
                            "failed_stops": int(row.get("failed_stops") or 0),
                            "last_completed_at": self._iso_datetime(row.get("last_completed_at")),
                            "last_status_update_at": self._iso_datetime(row.get("last_status_update_at")),
                        }
                    )
                return output

        rows = self.list_live_route_rows(target_date, force_refresh=force_refresh)
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            route_key = self._first_str(row.get("route_key")) or self._live_route_key(self._first_str(row.get("driver_name")))
            bucket = grouped.setdefault(
                route_key,
                {
                    "live_date": row.get("live_date"),
                    "route_key": route_key,
                    "driver_name": self._first_str(row.get("driver_name")) or "Unassigned",
                    "total_stops": 0,
                    "done_stops": 0,
                    "current_stops": 0,
                    "upcoming_stops": 0,
                    "failed_stops": 0,
                    "last_completed_at": "",
                    "last_status_update_at": "",
                },
            )
            bucket["total_stops"] += 1
            state = self._first_str(row.get("progress_state")) or "upcoming"
            if state == "done":
                bucket["done_stops"] += 1
                completed_at = self._first_str(row.get("delivered_at"))
                if completed_at and completed_at > bucket["last_completed_at"]:
                    bucket["last_completed_at"] = completed_at
            elif state == "current":
                bucket["current_stops"] += 1
            elif state == "failed":
                bucket["failed_stops"] += 1
            else:
                bucket["upcoming_stops"] += 1
            status_updated = self._first_str(row.get("status_updated_at"))
            if status_updated and status_updated > bucket["last_status_update_at"]:
                bucket["last_status_update_at"] = status_updated

        output = list(grouped.values())
        output.sort(key=lambda row: ((row.get("driver_name") or "").lower(), row.get("route_key") or ""))
        return output

    def get_live_progress_snapshot(self, live_date: str | date | None = None, force_refresh: bool = False) -> Dict[str, Any]:
        target_date = self._coerce_date(live_date)
        if not target_date:
            target_date = datetime.now(ZoneInfo(settings.BUSINESS_TZ)).date()

        route_rows = self.list_live_route_rows(target_date, force_refresh=force_refresh)
        driver_rows = self.list_live_driver_progress(target_date, force_refresh=force_refresh)

        routes_by_key: Dict[str, Dict[str, Any]] = {}
        counts = {"done": 0, "current": 0, "upcoming": 0, "failed": 0}
        latest_update = ""

        for row in route_rows:
            route_key = self._first_str(row.get("route_key")) or self._live_route_key(self._first_str(row.get("driver_name")))
            route = routes_by_key.setdefault(
                route_key,
                {
                    "route_key": route_key,
                    "driver_name": self._first_str(row.get("driver_name")) or "Unassigned",
                    "deliveries": [],
                },
            )
            route["deliveries"].append(
                {
                    "delivery_id": self._first_str(row.get("delivery_id")),
                    "jobsite_id": self._first_str(row.get("jobsite_id")),
                    "job_name": self._first_str(row.get("job_name")),
                    "job_address": self._first_str(row.get("job_address")),
                    "sales_order": self._first_str(row.get("sales_order")),
                    "packing_slip": self._first_str(row.get("packing_slip")),
                    "route_sequence": row.get("route_sequence"),
                    "status_code": self._first_str(row.get("status_code")),
                    "status_label": self._first_str(row.get("status_label")) or "Upcoming",
                    "status": self._first_str(row.get("progress_state")) or "upcoming",
                    "status_time": self._first_str(row.get("status_time")),
                    "status_updated_at": self._first_str(row.get("status_updated_at")),
                    "arrived_at": self._first_str(row.get("arrived_at")),
                    "delivered_at": self._first_str(row.get("delivered_at")),
                    "driver_latitude": row.get("driver_latitude"),
                    "driver_longitude": row.get("driver_longitude"),
                    "med_skid": self._coerce_int(row.get("med_skid")) or 0,
                    "small_skid": self._coerce_int(row.get("small_skid")) or 0,
                    "large_skid": self._coerce_int(row.get("large_skid")) or 0,
                    "door": self._coerce_int(row.get("door")) or 0,
                    "frame": self._coerce_int(row.get("frame")) or 0,
                    "exp_frame": self._coerce_int(row.get("exp_frame")) or 0,
                    "box": self._coerce_int(row.get("box")) or 0,
                    "pickup": self._to_bool(row.get("pickup")),
                    "pickup_vehicle": self._first_str(row.get("pickup_vehicle")),
                    "requires_empty_truck": self._to_bool(row.get("requires_empty_truck")),
                    "window_start": self._first_str(row.get("window_start")),
                    "window_end": self._first_str(row.get("window_end")),
                    "delivery_notes": self._first_str(row.get("delivery_notes")),
                }
            )

            state = self._first_str(row.get("progress_state")) or "upcoming"
            if state not in counts:
                state = "upcoming"
            counts[state] += 1
            row_status_update = self._first_str(row.get("status_updated_at"))
            if row_status_update and row_status_update > latest_update:
                latest_update = row_status_update

        driver_lookup = {self._first_str(item.get("route_key")): item for item in driver_rows}
        routes: List[Dict[str, Any]] = []
        for route_key, route in routes_by_key.items():
            route_deliveries = route.get("deliveries") or []
            route_deliveries.sort(
                key=lambda d: (
                    d.get("route_sequence") if d.get("route_sequence") is not None else 2147483647,
                    d.get("delivery_id") or "",
                )
            )
            stats = driver_lookup.get(route_key) or {}
            total = int(stats.get("total_stops") or len(route_deliveries))
            done = int(stats.get("done_stops") or 0)
            current = int(stats.get("current_stops") or 0)
            upcoming = int(stats.get("upcoming_stops") or 0)
            failed = int(stats.get("failed_stops") or 0)
            completion_pct = int(round((done / total) * 100)) if total > 0 else 0
            routes.append(
                {
                    "route_key": route_key,
                    "driver_name": self._first_str(route.get("driver_name")) or "Unassigned",
                    "total_stops": total,
                    "done_stops": done,
                    "current_stops": current,
                    "upcoming_stops": upcoming,
                    "failed_stops": failed,
                    "completion_pct": completion_pct,
                    "last_completed_at": self._first_str(stats.get("last_completed_at")),
                    "last_status_update_at": self._first_str(stats.get("last_status_update_at")),
                    "deliveries": route_deliveries,
                }
            )

        routes.sort(key=lambda item: (item.get("driver_name") or "").lower())
        total = counts["done"] + counts["current"] + counts["upcoming"] + counts["failed"]

        return {
            "date": target_date.isoformat(),
            "summary": {
                "done": counts["done"],
                "current": counts["current"],
                "upcoming": counts["upcoming"],
                "failed": counts["failed"],
                "total": total,
                "completion_pct": int(round((counts["done"] / total) * 100)) if total > 0 else 0,
            },
            "routes": routes,
            "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "last_status_update_at": latest_update or None,
        }

    def get_user_by_email(self, email: str) -> Dict[str, Any] | None:
        normalized = self._normalize_email(email)
        if not normalized:
            return None
        with SessionLocal() as session:
            if settings.AUTH_DEBUG:
                try:
                    logger.info(
                        "Auth debug DB=%s lookup_email=%s",
                        engine.url.render_as_string(hide_password=True),
                        normalized,
                    )
                except Exception as e:
                    logger.info("Auth debug DB url unavailable: %s", e)
            try:
                if self._table_exists("users", session.bind):
                    users_cols = self._users_column_names(session.bind)
                    required = {"email", "password_hash", "role", "active"}
                    if required.issubset(users_cols):
                        select_cols = ["email", "password_hash", "role", "active"]
                        if "name" in users_cols:
                            select_cols.append("name")
                        if "full_name" in users_cols:
                            select_cols.append("full_name")
                        if "session_token" in users_cols:
                            select_cols.append("session_token")
                        if "session_expires" in users_cols:
                            select_cols.append("session_expires")
                        row = session.execute(
                            text(
                                f"""
                                SELECT {", ".join(select_cols)}
                                FROM users
                                WHERE lower(trim(email)) = :email
                                LIMIT 1
                                """
                            ),
                            {"email": normalized},
                        ).mappings().first()
                        user_record = self._user_record_from_users_row(dict(row)) if row else None
                    else:
                        user_record = None
                    if settings.AUTH_DEBUG:
                        if user_record:
                            fields = user_record.get("fields", {})
                            logger.info(
                                "Auth debug DB user row email=%s active=%s",
                                fields.get(settings.USERS_EMAIL_FIELD),
                                fields.get(settings.USERS_ACTIVE_FIELD),
                            )
                        else:
                            logger.info("Auth debug DB user row not found for email=%s", normalized)
                    if user_record:
                        return user_record

                if self._table_exists("Users", session.bind):
                    user_record = self._query_legacy_user_by_email(session, normalized)
                    if settings.AUTH_DEBUG:
                        logger.info("Auth debug legacy Users lookup found=%s", bool(user_record))
                    return user_record

                if settings.AUTH_DEBUG:
                    logger.info("Auth debug users storage not found for email=%s", normalized)
                return None
            except SQLAlchemyError:
                logger.exception("Failed to query user by email.")
                return None

    def get_user_by_session_token(self, token: str) -> Dict[str, Any] | None:
        if not token:
            return None
        with SessionLocal() as session:
            try:
                if self._table_exists("users", session.bind):
                    users_cols = self._users_column_names(session.bind)
                    if self._table_exists("auth_sessions", session.bind) and "user_id" in users_cols:
                        select_cols = [
                            "u.email AS email",
                            "u.password_hash AS password_hash",
                            "u.role AS role",
                            "u.active AS active",
                            "s.session_token AS session_token",
                            "s.expires_at AS session_expires",
                        ]
                        if "name" in users_cols:
                            select_cols.append("u.name AS name")
                        if "full_name" in users_cols:
                            select_cols.append("u.full_name AS full_name")

                        row = session.execute(
                            text(
                                f"""
                                SELECT {", ".join(select_cols)}
                                FROM auth_sessions s
                                JOIN users u ON u.user_id = s.user_id
                                WHERE s.session_token = :token
                                  AND s.revoked_at IS NULL
                                  AND (s.expires_at IS NULL OR s.expires_at > NOW())
                                LIMIT 1
                                """
                            ),
                            {"token": token},
                        ).mappings().first()
                        if row:
                            try:
                                session.execute(
                                    text(
                                        """
                                        UPDATE auth_sessions
                                        SET last_seen_at = NOW()
                                        WHERE session_token = :token
                                        """
                                    ),
                                    {"token": token},
                                )
                                session.commit()
                            except Exception:
                                session.rollback()
                            return self._user_record_from_users_row(dict(row))

                    if "session_token" in users_cols:
                        select_cols = [
                            "email",
                            "password_hash",
                            "role",
                            "active",
                            "session_token",
                        ]
                        if "session_expires" in users_cols:
                            select_cols.append("session_expires")
                        else:
                            select_cols.append("NULL AS session_expires")
                        if "name" in users_cols:
                            select_cols.append("name")
                        if "full_name" in users_cols:
                            select_cols.append("full_name")
                        row = session.execute(
                            text(
                                f"""
                                SELECT {", ".join(select_cols)}
                                FROM users
                                WHERE session_token = :token
                                LIMIT 1
                                """
                            ),
                            {"token": token},
                        ).mappings().first()
                        if row:
                            return self._user_record_from_users_row(dict(row))
                if self._table_exists("Users", session.bind):
                    return self._query_legacy_user_by_session_token(session, token)
                return None
            except SQLAlchemyError:
                logger.exception("Failed to query user by session token.")
                return None

    def list_users_min(self, role: str | None = None, active_only: bool = True, force_refresh: bool = False) -> List[Dict[str, Any]]:
        records = self.list_records(settings.USERS_TABLE, force_refresh=force_refresh)
        out: List[Dict[str, Any]] = []
        for rec in records:
            fields = rec.get("fields", {})
            role_val = self._first_str(fields.get(settings.USERS_ROLE_FIELD))
            if role and role_val.lower() != str(role).lower():
                continue
            active_val = fields.get(settings.USERS_ACTIVE_FIELD, True)
            if active_only and not self._to_bool(active_val):
                continue
            name_raw = self._first_str(fields.get(settings.USERS_NAME_FIELD))
            email = self._first_str(fields.get(settings.USERS_EMAIL_FIELD))
            out.append(
                {
                    "record_id": rec.get("id"),
                    "name": name_raw,
                    "display_name": name_raw or email,
                    "email": email,
                    "role": role_val,
                    "active": self._to_bool(active_val),
                }
            )
        out.sort(key=lambda x: (x.get("name") or "").lower())
        return out

    def create_user(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        return self.create_record(settings.USERS_TABLE, fields)

    def update_user_session(self, record_id: str, token: str | None, expires_iso: str | None):
        normalized = self._normalize_email(record_id)
        raw_record_id = str(record_id or "").strip()
        if not normalized and not raw_record_id:
            raise ValueError("Invalid user record id.")
        expires_dt = self._coerce_datetime(expires_iso)
        with SessionLocal() as session:
            if self._table_exists("users", session.bind):
                users_cols = self._users_column_names(session.bind)
                if self._table_exists("auth_sessions", session.bind) and "user_id" in users_cols:
                    user_row = None
                    if normalized:
                        user_row = session.execute(
                            text(
                                """
                                SELECT user_id, email
                                FROM users
                                WHERE lower(trim(email)) = :email
                                LIMIT 1
                                """
                            ),
                            {"email": normalized},
                        ).mappings().first()
                    if not user_row and raw_record_id.isdigit():
                        user_row = session.execute(
                            text(
                                """
                                SELECT user_id, email
                                FROM users
                                WHERE user_id = :user_id
                                LIMIT 1
                                """
                            ),
                            {"user_id": int(raw_record_id)},
                        ).mappings().first()
                    if not user_row:
                        raise ValueError("Record not found.")

                    user_id = user_row.get("user_id")
                    user_email = self._normalize_email(user_row.get("email")) or self._first_str(user_row.get("email"))

                    session.execute(
                        text(
                            """
                            UPDATE auth_sessions
                            SET revoked_at = NOW()
                            WHERE user_id = :user_id
                              AND revoked_at IS NULL
                            """
                        ),
                        {"user_id": user_id},
                    )
                    if token:
                        session.execute(
                            text(
                                """
                                INSERT INTO auth_sessions (
                                    user_id,
                                    session_token,
                                    expires_at,
                                    created_at,
                                    last_seen_at
                                )
                                VALUES (
                                    :user_id,
                                    :token,
                                    :expires_at,
                                    NOW(),
                                    NOW()
                                )
                                """
                            ),
                            {
                                "user_id": user_id,
                                "token": token,
                                "expires_at": expires_dt,
                            },
                        )
                    session.commit()
                    result = self.get_user_by_email(user_email) if user_email else None
                elif "session_token" in users_cols:
                    if not normalized:
                        raise ValueError("Invalid user record id.")
                    set_parts = ["session_token = :token"]
                    params: Dict[str, Any] = {"token": token, "email": normalized}
                    if "session_expires" in users_cols:
                        set_parts.append("session_expires = :expires")
                        params["expires"] = expires_dt
                    if "updated_at" in users_cols:
                        set_parts.append("updated_at = NOW()")
                    update_result = session.execute(
                        text(
                            f"""
                            UPDATE users
                            SET {", ".join(set_parts)}
                            WHERE lower(trim(email)) = :email
                            """
                        ),
                        params,
                    )
                    if (update_result.rowcount or 0) <= 0:
                        raise ValueError("Record not found.")
                    session.commit()
                    result = self.get_user_by_email(normalized)
                else:
                    raise ValueError("Users storage does not support session fields.")
            elif self._table_exists("Users", session.bind):
                update_result = session.execute(
                    text(
                        """
                        UPDATE "Users"
                        SET "Session Token" = :token,
                            "Session Expires" = :expires
                        WHERE lower(btrim("Email")) = :email
                        """
                    ),
                    {"token": token, "expires": expires_iso, "email": normalized},
                )
                if (update_result.rowcount or 0) <= 0:
                    raise ValueError("Record not found.")
                session.commit()
                result = self._query_legacy_user_by_email(session, normalized)
            else:
                raise ValueError("Users storage is not configured.")
        self._invalidate_cache(settings.USERS_TABLE)
        return result

    def get_delivery_by_business_id(self, delivery_id: str) -> Dict[str, Any] | None:
        if not delivery_id:
            return None
        try:
            delivery_pk = int(str(delivery_id).strip())
        except Exception:
            return None
        with SessionLocal() as session:
            delivery = session.get(Delivery, delivery_pk)
            if not delivery:
                return None
            return self._delivery_record(delivery)

    def update_delivery_by_business_id(self, delivery_id: str, fields: Dict[str, Any]) -> Dict[str, Any] | None:
        if not delivery_id:
            return None
        try:
            delivery_pk = int(str(delivery_id).strip())
        except Exception:
            return None
        with SessionLocal() as session:
            delivery = session.get(Delivery, delivery_pk)
            if not delivery:
                return None
            self._apply_delivery_fields(delivery, fields)
            if hasattr(delivery, "updated_at"):
                delivery.updated_at = datetime.now(ZoneInfo("UTC"))
            session.commit()
            session.refresh(delivery)
            self._invalidate_cache("Deliveries")
            return self._delivery_record(delivery)

    def update_job_coordinates(self, record_id: str, lat: float, lon: float):
        """
        Updates coordinates on Jobsites (if you store them there).
        """
        try:
            with SessionLocal() as session:
                jobsite = session.get(Jobsite, int(record_id))
                if not jobsite:
                    return None
                jobsite.latitude = float(lat)
                jobsite.longitude = float(lon)
                if hasattr(jobsite, "updated_at"):
                    jobsite.updated_at = datetime.now(ZoneInfo("UTC"))
                session.commit()
                session.refresh(jobsite)
                self._invalidate_cache("Jobsites")
                return self._jobsite_record(jobsite)
        except Exception as e:
            logger.warning("Error updating jobsite record %s: %s", record_id, e)
            return None

    def get_tomorrow_deliveries(
        self,
        force_refresh: bool = False,
        target_date: date | str | None = None,
    ) -> list[DeliveryJob]:
        """
        Reads Deliveries for the next delivery day (business day or next scheduled).
        Resolves job name/address from linked Jobsite record.
        Fallbacks:
          - If link missing, uses Deliveries fields "Job Name" and "Job Address" if they exist.
          - Delivery id shown in UI is the business "Delivery ID" if present, else Airtable record id.
        """
        deliveries_records = self.list_records("Deliveries", force_refresh=force_refresh)
        jobsites_records = self.list_records("Jobsites", force_refresh=force_refresh)

        jobsite_by_id: Dict[str, Dict[str, Any]] = {
            r.get("id"): r.get("fields", {}) for r in jobsites_records
        }

        if target_date:
            resolved = self._coerce_date(target_date)
            target_date = resolved or self._resolve_target_date(deliveries_records)
        else:
            target_date = self._resolve_target_date(deliveries_records)
        target_iso = target_date.isoformat()
        self._last_target_date = target_iso
        results: list[DeliveryJob] = []

        link_field = settings.DELIVERIES_JOB_LINK_FIELD  # e.g. "Job" or "job"

        


        def _to_int(v, default=0) -> int:
            try:
                if v is None:
                    return default
                return int(float(v))
            except Exception:
                return default

        def _safe_float(v) -> float:
            try:
                if v is None:
                    return 0.0
                if isinstance(v, list):
                    v = v[0] if v else 0
                return float(v)
            except Exception:
                return 0.0

        for rec in deliveries_records:
            fields = rec.get("fields", {})
            scheduled_date_raw = fields.get("Scheduled Date")
            if not scheduled_date_raw:
                continue

            scheduled_date = self._parse_airtable_date(scheduled_date_raw)
            if not scheduled_date or scheduled_date.isoformat() != target_iso:
                continue

            # Linked jobsite record id (array)
            jobsite_ids = fields.get(link_field) or []
            jobsite_id = jobsite_ids[0] if isinstance(jobsite_ids, list) and jobsite_ids else None
            jobsite_fields = jobsite_by_id.get(jobsite_id, {}) if jobsite_id else {}

            job_name = (
                self._first_str(jobsite_fields.get(settings.JOBSITES_NAME_FIELD))
                or self._first_str(fields.get("Job Name"))
            )

            job_address = (
                self._first_str(jobsite_fields.get(settings.JOBSITES_ADDRESS_FIELD))
                or self._first_str(fields.get("Job Address"))
            )
            sales_order = self._first_str(fields.get(settings.DELIVERIES_SALES_ORDER_FIELD))
            packing_slip = self._first_str(fields.get(settings.DELIVERIES_PACKING_SLIP_FIELD))
            driver = self._first_str(fields.get(settings.DELIVERIES_DRIVER_FIELD))

            # Prefer delivery business id field, fallback to Airtable record id
            delivery_record_id = rec.get("id")
            delivery_business_id = fields.get("Delivery ID") or delivery_record_id

            # Coords: prefer Delivery record, fallback to Jobsite coords
            lat = _safe_float(fields.get("Latitude")) or _safe_float(jobsite_fields.get(settings.JOBSITES_LAT_FIELD))
            lon = _safe_float(fields.get("Longitude")) or _safe_float(jobsite_fields.get(settings.JOBSITES_LON_FIELD))

            door = _to_int(fields.get("Door", 0))
            frame = _to_int(fields.get("Frame", 0))
            exp_frame = _to_int(fields.get("Exp Frame", 0))
            med_skid = _to_int(fields.get("Med Skid", fields.get("Skid", 0)))
            small_skid = _to_int(fields.get("Small Skid", 0))
            large_skid = _to_int(fields.get("Large Skid", 0))
            box = _to_int(fields.get("Box", 0))
            pickup = self._to_bool(fields.get("Pickup"))
            pickup_vehicle = self._normalize_pickup_vehicle(fields.get(settings.DELIVERIES_PICKUP_VEHICLE_FIELD))
            if pickup and not pickup_vehicle:
                pickup_vehicle = "both"
            if not pickup:
                pickup_vehicle = ""
            requires_empty_truck = self._to_bool(fields.get("Requires Empty Truck"))

            results.append(
                DeliveryJob(
                    id=str(delivery_business_id),
                    job_id=jobsite_id,
                    job=jobsite_id,
                    job_name=job_name,
                    job_address=job_address,
                    sales_order=sales_order,
                    packing_slip=packing_slip,
                    driver=driver,
                    door=door,
                    frame=frame,
                    exp_frame=exp_frame,
                    med_skid=med_skid,
                    small_skid=small_skid,
                    large_skid=large_skid,
                    pickup=pickup,
                    pickup_vehicle=pickup_vehicle,
                    requires_empty_truck=requires_empty_truck,
                    box=box,
                    lat=float(lat or 0.0),
                    lon=float(lon or 0.0),
                    window_start=fields.get("Window Start"),
                    window_end=fields.get("Window End"),
                )
            )

        return results

    def get_published_plan(self, plan_date: str, plan_type: str = "published") -> Dict[str, Any] | None:
        payload = self.get_plan_payload(plan_date, plan_type)
        if not payload:
            return None
        plan_date_val = payload.get("plan_date")
        plan_type_val = self._normalize_plan_type(payload.get("plan_type"), default="published")
        fields = {
            settings.PUBLISHED_PLANS_DATE_FIELD: plan_date_val,
            settings.PUBLISHED_PLANS_TYPE_FIELD: plan_type_val,
            settings.PUBLISHED_PLANS_FLEET_FIELD: self._json_dumps(payload.get("fleet", {})),
            settings.PUBLISHED_PLANS_ROUTES_FIELD: self._json_dumps(payload.get("routes", [])),
            settings.PUBLISHED_PLANS_DELIVERIES_FIELD: self._json_dumps(payload.get("deliveries", [])),
            settings.PUBLISHED_PLANS_META_FIELD: self._json_dumps(payload.get("meta", {})),
            settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: self._json_dumps(payload.get("driver_profiles", [])),
            settings.PUBLISHED_PLANS_STATUS_FIELD: payload.get("status"),
            settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD: payload.get("updated_by"),
            settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: payload.get("updated_at"),
        }
        rec_id = f"{plan_date_val}::{plan_type_val}" if plan_type_val != "published" else str(plan_date_val or "")
        return {"id": rec_id, "fields": fields, "createdTime": payload.get("updated_at")}

    def upsert_published_plan(self, plan_date: str, fields: Dict[str, Any], plan_type: str = "published") -> Dict[str, Any]:
        normalized_type = self._normalize_plan_type(plan_type, default="published")
        payload = {
            "routes": self._json_load(fields.get(settings.PUBLISHED_PLANS_ROUTES_FIELD), []),
            "deliveries": self._json_load(fields.get(settings.PUBLISHED_PLANS_DELIVERIES_FIELD), []),
            "meta": self._json_load(fields.get(settings.PUBLISHED_PLANS_META_FIELD), {}),
            "fleet": self._json_load(fields.get(settings.PUBLISHED_PLANS_FLEET_FIELD), {}),
            "driver_profiles": self._json_load(fields.get(settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD), []),
            "status": fields.get(settings.PUBLISHED_PLANS_STATUS_FIELD),
            "updated_at": fields.get(settings.PUBLISHED_PLANS_UPDATED_AT_FIELD),
            "updated_by": fields.get(settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD),
        }
        saved = self.upsert_plan_payload(plan_date, normalized_type, payload)
        return {
            "id": f"{saved.get('plan_date')}::{saved.get('plan_type')}",
            "fields": {
                settings.PUBLISHED_PLANS_DATE_FIELD: saved.get("plan_date"),
                settings.PUBLISHED_PLANS_TYPE_FIELD: saved.get("plan_type"),
                settings.PUBLISHED_PLANS_FLEET_FIELD: self._json_dumps(saved.get("fleet", {})),
                settings.PUBLISHED_PLANS_ROUTES_FIELD: self._json_dumps(saved.get("routes", [])),
                settings.PUBLISHED_PLANS_DELIVERIES_FIELD: self._json_dumps(saved.get("deliveries", [])),
                settings.PUBLISHED_PLANS_META_FIELD: self._json_dumps(saved.get("meta", {})),
                settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: self._json_dumps(saved.get("driver_profiles", [])),
                settings.PUBLISHED_PLANS_STATUS_FIELD: saved.get("status"),
                settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD: saved.get("updated_by"),
                settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: saved.get("updated_at"),
            },
            "createdTime": saved.get("updated_at"),
        }

    def list_published_plans(self, force_refresh: bool = False, plan_type: str = "published") -> List[Dict[str, Any]]:
        normalized_type = self._normalize_plan_type(plan_type, default="published")
        include_all_types = normalized_type in {"all", "*"}
        with SessionLocal() as session:
            if not self._supports_published_plan_type(session.bind):
                if not self._table_exists("Published Plans", session.bind):
                    return []
                has_legacy_type = self._legacy_published_plan_has_type(session.bind)
                if normalized_type not in {"published", "all", "*"} and not has_legacy_type:
                    return []
                params: Dict[str, Any] = {}
                type_expr = (
                    "COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') AS \"Plan Type\""
                    if has_legacy_type
                    else "'published' AS \"Plan Type\""
                )
                where_clause = ""
                if not include_all_types:
                    if has_legacy_type:
                        where_clause = "WHERE COALESCE(NULLIF(btrim(\"Plan Type\"), ''), 'published') = :plan_type"
                        params["plan_type"] = normalized_type
                    elif normalized_type != "published":
                        return []
                rows = session.execute(
                    text(
                        f"""
                        SELECT
                            "Plan Date",
                            {type_expr},
                            "Fleet JSON",
                            "Routes JSON",
                            "Deliveries JSON",
                            "Meta JSON",
                            "Driver Profiles JSON",
                            "Status",
                            "Published By",
                            "Published At"
                        FROM "Published Plans"
                        {where_clause}
                        ORDER BY "Plan Date" DESC
                        """
                    ),
                    params,
                ).mappings().all()
                return [self._legacy_plan_record(dict(row)) for row in rows]
            if include_all_types:
                stmt = select(PublishedPlan)
            else:
                stmt = select(PublishedPlan).where(PublishedPlan.plan_type == normalized_type)
            if normalized_type == "published":
                stmt = select(PublishedPlan).where(
                    (PublishedPlan.plan_type == normalized_type) | (PublishedPlan.plan_type.is_(None))
                )
            records = session.execute(stmt.order_by(PublishedPlan.plan_date.desc())).scalars().all()
            return [self._plan_record(rec) for rec in records]


class MapboxService:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _CACHE_DIR = os.getenv("MATRIX_CACHE_DIR", os.path.join(_BASE_DIR, ".cache"))
    _MATRIX_VALIDATION_VERSION = 2
    _LAST_CACHE_CLEANUP = 0.0

    @classmethod
    def _matrix_cache_key(cls, coords_list) -> str:
        normalized = [[round(float(lon), 6), round(float(lat), 6)] for lon, lat in coords_list]
        payload = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _matrix_cache_path(cls, key: str) -> str:
        return os.path.join(cls._CACHE_DIR, f"matrix_{key}.json")

    @classmethod
    def _cache_ttl_seconds(cls) -> int:
        try:
            return max(0, int(settings.MATRIX_CACHE_TTL_SECONDS))
        except Exception:
            return 0

    @classmethod
    def _cache_cleanup_interval_seconds(cls) -> int:
        try:
            return max(60, int(settings.MATRIX_CACHE_CLEANUP_INTERVAL_SECONDS))
        except Exception:
            return 3600

    @classmethod
    def _is_cache_expired(cls, path: str, now: float | None = None) -> bool:
        ttl = cls._cache_ttl_seconds()
        if ttl <= 0:
            return False
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return False
        if now is None:
            now = time.time()
        return (now - mtime) > ttl

    @classmethod
    def _cleanup_matrix_cache(cls):
        ttl = cls._cache_ttl_seconds()
        if ttl <= 0:
            return
        now = time.time()
        with _MATRIX_CACHE_LOCK:
            interval = cls._cache_cleanup_interval_seconds()
            if (now - cls._LAST_CACHE_CLEANUP) < interval:
                return
            cls._LAST_CACHE_CLEANUP = now
            try:
                if not os.path.isdir(cls._CACHE_DIR):
                    return
                for name in os.listdir(cls._CACHE_DIR):
                    if not (name.startswith("matrix_") and name.endswith(".json")):
                        continue
                    path = os.path.join(cls._CACHE_DIR, name)
                    try:
                        if cls._is_cache_expired(path, now=now):
                            os.remove(path)
                    except FileNotFoundError:
                        continue
            except Exception as e:
                logger.warning("Error cleaning matrix cache: %s", e)

    @classmethod
    def _load_matrix_cache(cls, key: str):
        path = cls._matrix_cache_path(key)
        if not os.path.exists(path):
            return None
        if cls._is_cache_expired(path):
            try:
                os.remove(path)
            except Exception:
                pass
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "durations" in data and "distances" in data:
                return data
        except Exception as e:
            logger.warning("Error reading matrix cache %s: %s", path, e)
        return None

    @classmethod
    def _save_matrix_cache(cls, key: str, data: Dict[str, Any]):
        try:
            os.makedirs(cls._CACHE_DIR, exist_ok=True)
            path = cls._matrix_cache_path(key)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            payload = json.dumps(data)
            with _MATRIX_CACHE_LOCK:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                # Retry replace a few times to handle Windows file locks/AV scanners.
                for attempt in range(3):
                    try:
                        os.replace(tmp, path)
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05)
        except Exception as e:
            logger.warning("Error writing matrix cache %s: %s", key, e)

    @classmethod
    def _directions_cache_key(cls, lon1: float, lat1: float, lon2: float, lat2: float) -> str:
        payload = json.dumps(
            [round(float(lon1), 6), round(float(lat1), 6), round(float(lon2), 6), round(float(lat2), 6)],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _directions_cache_path(cls, key: str) -> str:
        return os.path.join(cls._CACHE_DIR, f"directions_{key}.json")

    @classmethod
    def _load_directions_cache(cls, key: str):
        path = cls._directions_cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "distance_m" in data and "duration_s" in data:
                return data
        except Exception as e:
            logger.warning("Error reading directions cache %s: %s", path, e)
        return None

    @classmethod
    def _save_directions_cache(cls, key: str, data: Dict[str, Any]):
        try:
            os.makedirs(cls._CACHE_DIR, exist_ok=True)
            path = cls._directions_cache_path(key)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            payload = json.dumps(data)
            with _MATRIX_CACHE_LOCK:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                for attempt in range(3):
                    try:
                        os.replace(tmp, path)
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05)
        except Exception as e:
            logger.warning("Error writing directions cache %s: %s", key, e)

    @staticmethod
    def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        # Great-circle distance in meters.
        r = 6371000.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dlon / 2) ** 2)
        return 2 * r * math.asin(math.sqrt(max(0.0, a)))

    @classmethod
    def _build_haversine_matrix_m(cls, coords_list) -> List[List[int]]:
        n = len(coords_list or [])
        out: List[List[int]] = [[0 for _ in range(n)] for _ in range(n)]
        for i in range(n):
            lon1, lat1 = coords_list[i]
            for j in range(i + 1, n):
                lon2, lat2 = coords_list[j]
                m = int(round(cls._haversine_m(float(lon1), float(lat1), float(lon2), float(lat2))))
                out[i][j] = m
                out[j][i] = m
        return out

    @staticmethod
    def _safe_matrix_value(mat, i: int, j: int) -> float | None:
        try:
            v = mat[i][j]
        except Exception:
            return None
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    @classmethod
    def _get_directions_distance_duration(cls, lon1: float, lat1: float, lon2: float, lat2: float):
        cache_key = cls._directions_cache_key(lon1, lat1, lon2, lat2)
        cached = cls._load_directions_cache(cache_key)
        if cached:
            return int(cached.get("distance_m") or 0), int(cached.get("duration_s") or 0), "cache"

        coords_str = f"{lon1},{lat1};{lon2},{lat2}"
        url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{coords_str}"
        params = {
            "access_token": settings.MAPBOX_TOKEN,
            "alternatives": "true",
            "overview": "false",
            "steps": "false",
        }
        resp = _HTTP_SESSION.get(url, params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
        logger.debug("Directions API called for %s", coords_str)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        routes = (data or {}).get("routes") or []
        if not routes:
            raise ValueError("Directions API returned no routes.")
        best_route = None
        best_distance = None
        for route in routes:
            if not isinstance(route, dict):
                continue
            try:
                route_distance = float(route.get("distance") or 0)
            except Exception:
                continue
            if route_distance <= 0:
                continue
            if best_route is None or best_distance is None or route_distance < best_distance:
                best_route = route
                best_distance = route_distance
        if best_route is None:
            raise ValueError("Directions API returned routes without valid distance.")
        dist = int(float(best_route.get("distance") or 0))
        dur = int(float(best_route.get("duration") or 0))
        cls._save_directions_cache(cache_key, {"distance_m": dist, "duration_s": dur})
        logger.debug("Directions result distance_m=%s duration_s=%s", dist, dur)
        return dist, dur, "api"

    @classmethod
    def _validate_matrix_distances(cls, coords_list, matrix_data: Dict[str, Any]) -> Dict[str, Any]:
        if not settings.MATRIX_VALIDATE_WITH_HAVERSINE:
            return matrix_data
        if not isinstance(matrix_data, dict):
            return matrix_data

        distances = matrix_data.get("distances")
        durations = matrix_data.get("durations")
        if not isinstance(distances, list) or not isinstance(durations, list):
            return matrix_data

        n = len(coords_list or [])
        if n == 0:
            return matrix_data

        factor = max(1.01, float(settings.MATRIX_HAVERSINE_FACTOR))
        min_baseline_m = max(1.0, float(settings.MATRIX_HAVERSINE_MIN_BASELINE_M))
        max_corrections = max(0, int(settings.MATRIX_DIRECTIONS_MAX_CORRECTIONS))
        raw_haversine_flag_factor = max(1.0, float(settings.MATRIX_RAW_HAVERSINE_FACTOR_THRESHOLD))

        haversine_m = cls._build_haversine_matrix_m(coords_list)
        corrections: List[Dict[str, Any]] = []
        checked_pairs = 0
        corrected_pairs = 0
        raw_haversine_flagged_pairs = 0
        raw_haversine_flags: List[Dict[str, Any]] = []

        for i in range(n):
            lon1, lat1 = coords_list[i]
            for j in range(n):
                if i == j:
                    continue
                checked_pairs += 1
                matrix_dist = cls._safe_matrix_value(distances, i, j)
                if matrix_dist is None or matrix_dist <= 0:
                    continue
                hv = float(haversine_m[i][j])
                raw_flag = False
                raw_factor = None
                if hv > 0:
                    raw_factor = float(matrix_dist) / hv
                    if raw_factor > raw_haversine_flag_factor:
                        raw_flag = True
                        raw_haversine_flagged_pairs += 1
                        raw_haversine_flags.append(
                            {
                                "from_node": i,
                                "to_node": j,
                                "matrix_m": int(matrix_dist),
                                "haversine_m": int(hv),
                                "factor": round(raw_factor, 6),
                            }
                        )
                baseline = max(hv, min_baseline_m)
                baseline_flag = float(matrix_dist) > (factor * baseline)
                if not raw_flag and not baseline_flag:
                    continue
                if corrected_pairs >= max_corrections:
                    continue

                lon2, lat2 = coords_list[j]
                try:
                    dir_dist, dir_dur, source = cls._get_directions_distance_duration(
                        float(lon1), float(lat1), float(lon2), float(lat2)
                    )
                except Exception as e:
                    corrections.append(
                        {
                            "from_node": i,
                            "to_node": j,
                            "matrix_m": int(matrix_dist),
                            "haversine_m": int(hv),
                            "trigger": "raw_haversine" if raw_flag else "baseline_factor",
                            "raw_factor": round(raw_factor, 6) if raw_factor is not None else None,
                            "error": str(e),
                            "replaced": False,
                        }
                    )
                    continue

                if dir_dist >= 0:
                    distances[i][j] = int(dir_dist)
                    corrected_pairs += 1
                    corrections.append(
                        {
                            "from_node": i,
                            "to_node": j,
                            "matrix_m": int(matrix_dist),
                            "haversine_m": int(hv),
                            "trigger": "raw_haversine" if raw_flag else "baseline_factor",
                            "raw_factor": round(raw_factor, 6) if raw_factor is not None else None,
                            "directions_m": int(dir_dist),
                            "directions_s": int(dir_dur),
                            "source": source,
                            "replaced": True,
                        }
                    )

        matrix_data["haversine_distances_m"] = haversine_m
        matrix_data["distance_corrections"] = corrections
        matrix_data["raw_haversine_flags"] = raw_haversine_flags
        matrix_data["distance_correction_meta"] = {
            "enabled": bool(settings.MATRIX_VALIDATE_WITH_HAVERSINE),
            "validation_version": cls._MATRIX_VALIDATION_VERSION,
            "factor": factor,
            "min_baseline_m": int(min_baseline_m),
            "checked_pairs": checked_pairs,
            "replaced_pairs": corrected_pairs,
            "max_corrections": max_corrections,
            "raw_haversine_factor_threshold": raw_haversine_flag_factor,
            "raw_haversine_flagged_pairs": raw_haversine_flagged_pairs,
        }
        return matrix_data

    @staticmethod
    def _postal_prefix(postal: str) -> str:
        """
        Extract first 3 characters of a Canadian postal code (FSA).
        Returns uppercase string like 'M1J' or '' if not found.
        """
        if not postal:
            return ""
        
        t = postal.upper()
        # Match Canadian postal code anywhere, with optional space: "M3C1E7" or "M3C 1E7"
        m = re.search(r"([A-Z]\d[A-Z])\s*\d[A-Z]\d", t)
        #m = re.search(r"\b([A-Za-z]\d[A-Za-z])\b", postal.replace(" ", "").upper())
        return m.group(1) if m else ""

    @staticmethod
    def get_matrix(coords_list, force_refresh: bool = False):
        if not settings.MAPBOX_TOKEN:
            raise RuntimeError("Missing MAPBOX_TOKEN env var.")

        MapboxService._cleanup_matrix_cache()
        cache_key = MapboxService._matrix_cache_key(coords_list)
        if not force_refresh:
            cached = MapboxService._load_matrix_cache(cache_key)
            if cached:
                correction_meta = (cached or {}).get("distance_correction_meta") or {}
                expected_factor = max(1.01, float(settings.MATRIX_HAVERSINE_FACTOR))
                expected_min_baseline = int(max(1.0, float(settings.MATRIX_HAVERSINE_MIN_BASELINE_M)))
                expected_raw_threshold = max(1.0, float(settings.MATRIX_RAW_HAVERSINE_FACTOR_THRESHOLD))
                needs_upgrade = (
                    "distance_correction_meta" not in cached
                    or "raw_haversine_flagged_pairs" not in correction_meta
                    or "raw_haversine_flags" not in cached
                    or int(correction_meta.get("validation_version", 0) or 0) != int(MapboxService._MATRIX_VALIDATION_VERSION)
                    or float(correction_meta.get("factor", 0) or 0) != float(expected_factor)
                    or int(correction_meta.get("min_baseline_m", 0) or 0) != expected_min_baseline
                    or float(correction_meta.get("raw_haversine_factor_threshold", 0) or 0) != float(expected_raw_threshold)
                )
                if settings.MATRIX_VALIDATE_WITH_HAVERSINE and needs_upgrade:
                    upgraded = copy.deepcopy(cached)
                    if "mapbox_raw" not in upgraded:
                        upgraded["mapbox_raw"] = {
                            "distances": copy.deepcopy(upgraded.get("distances", [])),
                            "durations": copy.deepcopy(upgraded.get("durations", [])),
                        }
                    upgraded = MapboxService._validate_matrix_distances(coords_list, upgraded)
                    MapboxService._save_matrix_cache(cache_key, upgraded)
                    return upgraded
                return cached

        coords_str = ";".join([f"{lon},{lat}" for lon, lat in coords_list])
        url = f"https://api.mapbox.com/directions-matrix/v1/mapbox/driving/{coords_str}"
        params = {"access_token": settings.MAPBOX_TOKEN, "annotations": "duration,distance"}
        resp = _HTTP_SESSION.get(url, params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw_data = resp.json()
        if isinstance(raw_data, dict) and "durations" in raw_data and "distances" in raw_data:
            # Keep original Mapbox output for debugging, then apply optional distance correction.
            data = copy.deepcopy(raw_data)
            data["mapbox_raw"] = raw_data
            data = MapboxService._validate_matrix_distances(coords_list, data)
            MapboxService._save_matrix_cache(cache_key, data)
            return data
        return raw_data

    def get_coordinates(self,address: str):
        if not settings.MAPBOX_TOKEN:
            raise RuntimeError("Missing MAPBOX_TOKEN env var.")
        if not address:
            return None, None

        # Extract postal prefix from input address
        input_prefix = self._postal_prefix(address)

        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
        params = {
            "access_token": settings.MAPBOX_TOKEN,
            "limit": 5,  # explicitly ask for multiple results
        }

        try:
            resp = _HTTP_SESSION.get(url, params=params, timeout=settings.HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            if not features:
                return None, None

            # Try to find a feature with matching postal prefix
            for f in features:
                context = f.get("context", [])
                for c in context:
                    if c.get("id", "").startswith("postcode"):
                        result_prefix = self._postal_prefix(c.get("text", ""))
                        logger.debug("Mapbox postal prefix match: input=%s result=%s", input_prefix, result_prefix)
                        if input_prefix and result_prefix == input_prefix:
                            lon, lat = f["center"]
                            return float(lat), float(lon)

            # Fallback: first feature (Mapbox best guess)
            lon, lat = features[0]["center"]
            return float(lat), float(lon)

        except Exception as e:
            logger.warning("Error geocoding '%s': %s", address, e)
            return None, None
