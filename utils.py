import logging
import re
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

from config import settings
from services import DatabaseService


def parse_sales_order(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    cleaned = raw.replace(",", "")
    if re.fullmatch(r"\d+(\.\d+)?", cleaned):
        return float(cleaned) if "." in cleaned else int(cleaned)
    return None


def to_int(value: str | int | float | None, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def business_date_iso(offset_days: int = 0) -> str:
    tz = ZoneInfo(settings.BUSINESS_TZ)
    return (datetime.now(tz).date() + timedelta(days=offset_days)).isoformat()


def next_plan_date_iso(logger: logging.Logger | None = None) -> str:
    strategy = (settings.NEXT_DELIVERY_STRATEGY or "business_day").strip().lower()
    if strategy == "next_scheduled":
        try:
            return DatabaseService().get_next_delivery_date()
        except Exception:
            if logger:
                logger.warning(
                    "Falling back to next_business_date_iso after get_next_delivery_date failure.",
                    exc_info=True,
                )
    return DatabaseService.next_business_date_iso()


def parse_iso_date(
    value: str | date | None,
    *,
    field_name: str = "date",
    allow_none: bool = True,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field_name} is required.")
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value).strip()
    if not raw:
        if allow_empty:
            return ""
        if allow_none:
            return None
        raise ValueError(f"{field_name} is required.")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format.")
