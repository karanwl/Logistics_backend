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

    @classmethod
    def _supports_published_plan_type(cls, bind=None) -> bool:
        if cls._published_plan_type_supported is not None:
            return cls._published_plan_type_supported
        try:
            inspector = inspect(bind or engine)
            cols = inspector.get_columns("Published Plans")
            col_names = {str(c.get("name", "")).strip() for c in cols}
            cls._published_plan_type_supported = "Plan Type" in col_names
        except Exception:
            cls._published_plan_type_supported = False
        return cls._published_plan_type_supported

    @staticmethod
    def _normalize_table_name(table_name: str) -> str:
        return (table_name or "").strip()

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
            settings.DELIVERIES_DRIVER_NOTES_FIELD: delivery.delivery_notes,
            "Window Start": delivery.window_start,
            "Window End": delivery.window_end,
            settings.DELIVERIES_PACKING_SLIP_FIELD: delivery.packing_slip,
        }
        return {"id": str(delivery.delivery_id), "fields": fields}

    def _user_record(self, user: User) -> Dict[str, Any]:
        fields = {
            settings.USERS_EMAIL_FIELD: user.email,
            settings.USERS_PASSWORD_FIELD: user.password_hash,
            settings.USERS_ROLE_FIELD: user.role,
            settings.USERS_ACTIVE_FIELD: user.active,
            settings.USERS_NAME_FIELD: user.name,
            settings.USERS_SESSION_TOKEN_FIELD: user.session_token,
            settings.USERS_SESSION_EXPIRES_FIELD: user.session_expires,
        }
        return {"id": user.email, "fields": fields}

    @staticmethod
    def _normalize_plan_type(value: str | None, default: str = "published") -> str:
        normalized = str(value or "").strip().lower()
        return normalized or default

    def _plan_record(self, plan: PublishedPlan) -> Dict[str, Any]:
        plan_date = plan.plan_date.isoformat() if plan.plan_date else None
        plan_type = self._normalize_plan_type(getattr(plan, "plan_type", None))
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
            settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: plan.published_at,
        }
        record_id = f"{plan_date}::{plan_type}" if plan_date else ""
        return {"id": record_id, "fields": fields, "createdTime": plan.published_at}

    def _legacy_plan_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        plan_date_val = row.get("Plan Date")
        plan_date = plan_date_val.isoformat() if hasattr(plan_date_val, "isoformat") else str(plan_date_val or "")
        fields = {
            settings.PUBLISHED_PLANS_DATE_FIELD: plan_date,
            settings.PUBLISHED_PLANS_TYPE_FIELD: "published",
            settings.PUBLISHED_PLANS_FLEET_FIELD: row.get("Fleet JSON"),
            settings.PUBLISHED_PLANS_ROUTES_FIELD: row.get("Routes JSON"),
            settings.PUBLISHED_PLANS_DELIVERIES_FIELD: row.get("Deliveries JSON"),
            settings.PUBLISHED_PLANS_META_FIELD: row.get("Meta JSON"),
            settings.PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: row.get("Driver Profiles JSON"),
            settings.PUBLISHED_PLANS_STATUS_FIELD: row.get("Status"),
            settings.PUBLISHED_PLANS_PUBLISHED_BY_FIELD: row.get("Published By"),
            settings.PUBLISHED_PLANS_UPDATED_AT_FIELD: row.get("Published At"),
        }
        return {"id": plan_date, "fields": fields, "createdTime": row.get("Published At")}

    def _plan_payload(self, plan: PublishedPlan) -> Dict[str, Any]:
        plan_date = plan.plan_date.isoformat() if plan.plan_date else None
        plan_type = self._normalize_plan_type(getattr(plan, "plan_type", None))
        return {
            "plan_date": plan_date,
            "plan_type": plan_type,
            "routes": self._json_load(plan.routes_json, []),
            "deliveries": self._json_load(plan.deliveries_json, []),
            "meta": self._json_load(plan.meta_json, {}),
            "fleet": self._json_load(plan.fleet_json, {}),
            "driver_profiles": self._json_load(plan.driver_profiles_json, []),
            "status": plan.status,
            "updated_at": plan.published_at,
            "updated_by": plan.published_by,
        }

    def _legacy_plan_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        plan_date_val = row.get("Plan Date")
        plan_date = plan_date_val.isoformat() if hasattr(plan_date_val, "isoformat") else str(plan_date_val or "")
        return {
            "plan_date": plan_date,
            "plan_type": "published",
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
            plan.published_at = fields.get("updated_at")
        if "updated_by" in fields:
            plan.published_by = fields.get("updated_by")

    def list_plan_meta(self, plan_type: str) -> List[Dict[str, Any]]:
        normalized_type = self._normalize_plan_type(plan_type)
        with SessionLocal() as session:
            if not self._supports_published_plan_type(session.bind):
                if normalized_type != "published":
                    return []
                rows = session.execute(
                    text(
                        """
                        SELECT
                            "Plan Date",
                            "Published At",
                            "Published By"
                        FROM "Published Plans"
                        ORDER BY "Plan Date" DESC
                        """
                    )
                ).mappings().all()
                return [
                    {
                        "plan_date": row.get("Plan Date").isoformat() if hasattr(row.get("Plan Date"), "isoformat") else str(row.get("Plan Date") or ""),
                        "plan_type": "published",
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
                if normalized_type != "published":
                    return None
                row = session.execute(
                    text(
                        """
                        SELECT
                            "Plan Date",
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
                        """
                    ),
                    {"plan_date": plan_dt},
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
                if normalized_type != "published":
                    raise ValueError(
                        'Published Plans schema is missing "Plan Type". Add the column before saving plan records.'
                    )
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
                    {
                        "plan_date": plan_dt,
                        "fleet_json": self._json_dumps(fields.get("fleet", {})),
                        "routes_json": self._json_dumps(fields.get("routes", [])),
                        "deliveries_json": self._json_dumps(fields.get("deliveries", [])),
                        "meta_json": self._json_dumps(fields.get("meta", {})),
                        "driver_profiles_json": self._json_dumps(fields.get("driver_profiles", [])),
                        "status": self._first_str(fields.get("status")) if fields.get("status") not in (None, "") else None,
                        "published_by": fields.get("updated_by"),
                        "published_at": fields.get("updated_at"),
                    },
                )
                session.commit()
                row = session.execute(
                    text(
                        """
                        SELECT
                            "Plan Date",
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
                        """
                    ),
                    {"plan_date": plan_dt},
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
            elif key == settings.DELIVERIES_DRIVER_NOTES_FIELD:
                delivery.delivery_notes = self._first_str(value) if value is not None else None
            elif key == "Window Start":
                delivery.window_start = self._first_str(value) if value not in (None, "") else None
            elif key == "Window End":
                delivery.window_end = self._first_str(value) if value not in (None, "") else None
            elif key == settings.DELIVERIES_PACKING_SLIP_FIELD:
                delivery.packing_slip = self._coerce_int(value)

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
                user.name = self._first_str(value) if value not in (None, "") else None
            elif key == settings.USERS_SESSION_TOKEN_FIELD:
                user.session_token = self._first_str(value) if value not in (None, "") else None
            elif key == settings.USERS_SESSION_EXPIRES_FIELD:
                user.session_expires = self._first_str(value) if value not in (None, "") else None

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
                plan.published_at = self._first_str(value) if value not in (None, "") else None

    def _get_record_by_pk(self, session, table_name: str, record_id: str):
        if table_name == "Deliveries":
            try:
                pk = int(record_id)
            except Exception:
                return None
            return session.get(Delivery, pk)
        if table_name == "Jobsites":
            try:
                pk = int(record_id)
            except Exception:
                return None
            return session.get(Jobsite, pk)
        if table_name == settings.USERS_TABLE:
            return session.get(User, str(record_id))
        if table_name == settings.PUBLISHED_PLANS_TABLE:
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
            if table_name == "Jobsites":
                records = session.execute(select(Jobsite)).scalars().all()
                data = [self._jobsite_record(r) for r in records]
                self._set_cached_table(table_name, data)
                return data
            if table_name == "Deliveries":
                records = session.execute(select(Delivery)).scalars().all()
                data = [self._delivery_record(r) for r in records]
                self._set_cached_table(table_name, data)
                return data
            if table_name == settings.USERS_TABLE:
                records = session.execute(select(User)).scalars().all()
                data = [self._user_record(r) for r in records]
                self._set_cached_table(table_name, data)
                return data
            if table_name == settings.PUBLISHED_PLANS_TABLE:
                data = self.list_published_plans(force_refresh=force_refresh, plan_type="published")
                self._set_cached_table(table_name, data)
                return data
        raise ValueError(f"Unsupported table: {table_name}")

    def get_record(self, table_name: str, record_id: str) -> Dict[str, Any]:
        with SessionLocal() as session:
            rec = self._get_record_by_pk(session, table_name, record_id)
            if not rec:
                raise ValueError("Record not found.")
            if table_name == "Jobsites":
                return self._jobsite_record(rec)
            if table_name == "Deliveries":
                return self._delivery_record(rec)
            if table_name == settings.USERS_TABLE:
                return self._user_record(rec)
            if table_name == settings.PUBLISHED_PLANS_TABLE:
                return self._plan_record(rec)
        raise ValueError(f"Unsupported table: {table_name}")

    def create_record(self, table_name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with SessionLocal() as session:
            if table_name == "Jobsites":
                jobsite = Jobsite(name="")
                self._apply_jobsite_fields(jobsite, fields)
                session.add(jobsite)
                session.commit()
                session.refresh(jobsite)
                self._invalidate_cache("Jobsites")
                return self._jobsite_record(jobsite)
            if table_name == "Deliveries":
                delivery = Delivery()
                self._apply_delivery_fields(delivery, fields)
                session.add(delivery)
                session.commit()
                session.refresh(delivery)
                self._invalidate_cache("Deliveries")
                return self._delivery_record(delivery)
            if table_name == settings.USERS_TABLE:
                user = User(email="")
                self._apply_user_fields(user, fields)
                session.add(user)
                session.commit()
                session.refresh(user)
                self._invalidate_cache(settings.USERS_TABLE)
                return self._user_record(user)
            if table_name == settings.PUBLISHED_PLANS_TABLE:
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
            if table_name == "Jobsites":
                self._apply_jobsite_fields(rec, fields)
                session.commit()
                session.refresh(rec)
                self._invalidate_cache("Jobsites")
                return self._jobsite_record(rec)
            if table_name == "Deliveries":
                self._apply_delivery_fields(rec, fields)
                session.commit()
                session.refresh(rec)
                self._invalidate_cache("Deliveries")
                return self._delivery_record(rec)
            if table_name == settings.USERS_TABLE:
                self._apply_user_fields(rec, fields)
                session.commit()
                session.refresh(rec)
                self._invalidate_cache(settings.USERS_TABLE)
                return self._user_record(rec)
            if table_name == settings.PUBLISHED_PLANS_TABLE:
                self._apply_plan_fields(rec, fields)
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
            if table_name == "Jobsites":
                payload = self._jobsite_record(rec)
            elif table_name == "Deliveries":
                payload = self._delivery_record(rec)
            elif table_name == settings.USERS_TABLE:
                payload = self._user_record(rec)
            elif table_name == settings.PUBLISHED_PLANS_TABLE:
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
            })

        out.sort(key=lambda x: (x["scheduled_date"] or "", x["delivery_id"] or ""))
        return out

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
            user = session.execute(
                select(User).where(func.lower(func.trim(User.email)) == normalized)
            ).scalar_one_or_none()
            if settings.AUTH_DEBUG:
                if user:
                    logger.info("Auth debug DB user row email=%s active=%s", user.email, user.active)
                else:
                    logger.info("Auth debug DB user row not found for email=%s", normalized)
            if not user:
                return None
            return self._user_record(user)

    def get_user_by_session_token(self, token: str) -> Dict[str, Any] | None:
        if not token:
            return None
        with SessionLocal() as session:
            user = session.execute(
                select(User).where(User.session_token == token)
            ).scalar_one_or_none()
            if not user:
                return None
            return self._user_record(user)

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
        fields = {
            settings.USERS_SESSION_TOKEN_FIELD: token,
            settings.USERS_SESSION_EXPIRES_FIELD: expires_iso,
        }
        result = self.update_record(settings.USERS_TABLE, record_id, fields)
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
                if normalized_type not in {"published", "all", "*"}:
                    return []
                rows = session.execute(
                    text(
                        """
                        SELECT
                            "Plan Date",
                            "Fleet JSON",
                            "Routes JSON",
                            "Deliveries JSON",
                            "Meta JSON",
                            "Driver Profiles JSON",
                            "Status",
                            "Published By",
                            "Published At"
                        FROM "Published Plans"
                        ORDER BY "Plan Date" DESC
                        """
                    )
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
