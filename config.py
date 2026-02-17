import os
from pydantic import BaseModel


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


class Settings(BaseModel):
    APP_NAME: str = os.getenv("APP_NAME", "Logistics Planner")
    APP_ENV: str = os.getenv("APP_ENV", "development")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    AUTH_DEBUG: bool = _env_bool("AUTH_DEBUG", "true")

    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_4p2cSfwajsKt@ep-calm-mud-ah28l0je-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
    DB_POOL_SIZE: int = _env_int("DB_POOL_SIZE", "5")
    DB_MAX_OVERFLOW: int = _env_int("DB_MAX_OVERFLOW", "10")

    MAPBOX_TOKEN: str = os.getenv("MAPBOX_TOKEN", "pk.eyJ1Ijoia2FyYW53bCIsImEiOiJjbWtwdms0cXYwbHBnM2twcGVoOHc0eDc4In0.E5jVqwGUtKnwMR10E1pg5w")
    MATRIX_VALIDATE_WITH_HAVERSINE: bool = _env_bool("MATRIX_VALIDATE_WITH_HAVERSINE", "true")
    MATRIX_HAVERSINE_FACTOR: float = _env_float("MATRIX_HAVERSINE_FACTOR", "4.0")
    MATRIX_RAW_HAVERSINE_FACTOR_THRESHOLD: float = _env_float("MATRIX_RAW_HAVERSINE_FACTOR_THRESHOLD", "1.6")
    MATRIX_HAVERSINE_MIN_BASELINE_M: float = _env_float("MATRIX_HAVERSINE_MIN_BASELINE_M", "250")
    MATRIX_DIRECTIONS_MAX_CORRECTIONS: int = _env_int("MATRIX_DIRECTIONS_MAX_CORRECTIONS", "120")
    MATRIX_CACHE_TTL_SECONDS: int = _env_int("MATRIX_CACHE_TTL_SECONDS", "604800")
    MATRIX_CACHE_CLEANUP_INTERVAL_SECONDS: int = _env_int("MATRIX_CACHE_CLEANUP_INTERVAL_SECONDS", "3600")

    DEPOT_LAT: float = _env_float("DEPOT_LAT", "43.768008")
    DEPOT_LON: float = _env_float("DEPOT_LON", "-79.569975")

    PHYSICAL_TRUCKS: int = _env_int("PHYSICAL_TRUCKS", "2")
    VEHICLE_CAPACITY_UNITS: int = _env_int("VEHICLE_CAPACITY_UNITS", "140")
    VAN_CAPACITY_UNITS: int = _env_int("VAN_CAPACITY_UNITS", "25")
    MED_SKID_UNITS: int = _env_int("MED_SKID_UNITS", os.getenv("LONG_SKID_UNITS", "20"))
    LONG_SKID_UNITS: int = _env_int("LONG_SKID_UNITS", "20")
    LARGE_SKID_UNITS: int = _env_int("LARGE_SKID_UNITS", "25")
    SMALL_SKID_UNITS: int = _env_int("SMALL_SKID_UNITS", "10")
    FRAME_UNITS: int = _env_int("FRAME_UNITS", "1")
    MAX_SKIDS: int = _env_int("MAX_SKIDS", "7")

    # --- Solver tuning knobs ---
    SOLVER_TRUCK_COST_FACTOR: float = _env_float("SOLVER_TRUCK_COST_FACTOR", "1.12")
    SOLVER_TRUCK_TIME_FACTOR: float = _env_float("SOLVER_TRUCK_TIME_FACTOR", "1.08")
    SOLVER_TRUCK_SERVE_VAN_ELIGIBLE_PENALTY: int = _env_int("SOLVER_TRUCK_SERVE_VAN_ELIGIBLE_PENALTY", "2500")
    SOLVER_TIME_SPAN_COST_COEFFICIENT: int = _env_int("SOLVER_TIME_SPAN_COST_COEFFICIENT", "5")
    SOLVER_DOWNTOWN_SOFT_LATEST_SECONDS: int = _env_int("SOLVER_DOWNTOWN_SOFT_LATEST_SECONDS", "14400")
    SOLVER_DOWNTOWN_HARD_LATEST_SECONDS: int = _env_int("SOLVER_DOWNTOWN_HARD_LATEST_SECONDS", "21600")
    SOLVER_DOWNTOWN_LATE_PENALTY_PER_SECOND: int = _env_int("SOLVER_DOWNTOWN_LATE_PENALTY_PER_SECOND", "60")
    SOLVER_VAN_FIXED_COST: int = _env_int("SOLVER_VAN_FIXED_COST", "0")
    SOLVER_TRUCK_FIXED_COST_PHYSICAL: int = _env_int("SOLVER_TRUCK_FIXED_COST_PHYSICAL", "500")
    SOLVER_TRUCK_FIXED_COST_OVERFLOW: int = _env_int("SOLVER_TRUCK_FIXED_COST_OVERFLOW", "60000")
    SOLVER_DROP_NODE_PENALTY: int = _env_int("SOLVER_DROP_NODE_PENALTY", "1000000")
    SOLVER_SHIFT_SECONDS: int = _env_int("SOLVER_SHIFT_SECONDS", "28800")
    SOLVER_WAITING_SLACK_SECONDS: int = _env_int("SOLVER_WAITING_SLACK_SECONDS", "600")
    SOLVER_TIME_LIMIT_SECONDS: int = _env_int("SOLVER_TIME_LIMIT_SECONDS", "30")
    SOLVER_RELOAD_THRESHOLD_UNITS: int = _env_int(
        "SOLVER_RELOAD_THRESHOLD_UNITS",
        os.getenv("VEHICLE_CAPACITY_UNITS", "140"),
    )

    DT_CENTER_LAT: float = _env_float("DT_CENTER_LAT", "43.640666")
    DT_CENTER_LON: float = _env_float("DT_CENTER_LON", "-79.376530")
    DT_RADIUS_KM: float = _env_float("DT_RADIUS_KM", "4.0")

    UPDATE_AIRTABLE_COORDS: bool = _env_bool("UPDATE_AIRTABLE_COORDS", "true")
    ALLOW_HOMEPAGE_CRUD: bool = _env_bool("ALLOW_HOMEPAGE_CRUD", "false")

    BUSINESS_TZ: str = os.getenv("BUSINESS_TZ", "UTC")
    BUSINESS_WEEKDAYS: str = os.getenv("BUSINESS_WEEKDAYS", "0,1,2,3,4")
    HOLIDAY_DATES: str = os.getenv("HOLIDAY_DATES", "")
    NEXT_DELIVERY_STRATEGY: str = os.getenv("NEXT_DELIVERY_STRATEGY", "business_day")
    NEXT_DELIVERY_LOOKAHEAD_DAYS: int = _env_int("NEXT_DELIVERY_LOOKAHEAD_DAYS", "30")

    SESSION_DAYS: int = _env_int("SESSION_DAYS", "7")
    AUTH_SESSION_CACHE_SECONDS: int = _env_int("AUTH_SESSION_CACHE_SECONDS", "300")
    AUTH_COOKIE_NAME: str = os.getenv("AUTH_COOKIE_NAME", "auth_token")
    AUTH_COOKIE_SECURE: bool = _env_bool("AUTH_COOKIE_SECURE", "false")
    AUTH_COOKIE_SAMESITE: str = os.getenv("AUTH_COOKIE_SAMESITE", "lax")
    AUTH_COOKIE_DOMAIN: str | None = os.getenv("AUTH_COOKIE_DOMAIN") or None
    AUTH_COOKIE_PATH: str = os.getenv("AUTH_COOKIE_PATH", "/")

    CSRF_ENABLED: bool = _env_bool("CSRF_ENABLED", "true")
    CSRF_COOKIE_NAME: str = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
    CSRF_HEADER_NAME: str = os.getenv("CSRF_HEADER_NAME", "X-CSRF-Token")
    CSRF_COOKIE_SECURE: bool = _env_bool("CSRF_COOKIE_SECURE", "false")
    CSRF_COOKIE_SAMESITE: str = os.getenv("CSRF_COOKIE_SAMESITE", "lax")

    HTTP_TIMEOUT_SECONDS: int = _env_int("HTTP_TIMEOUT_SECONDS", "30")
    HTTP_RETRIES: int = _env_int("HTTP_RETRIES", "3")
    HTTP_RETRY_BACKOFF: float = _env_float("HTTP_RETRY_BACKOFF", "0.5")
    FCM_SERVER_KEY: str = os.getenv("FCM_SERVER_KEY", "")
    FCM_SEND_URL: str = os.getenv("FCM_SEND_URL", "https://fcm.googleapis.com/fcm/send")

    # --- Airtable schema knobs (important for your setup) ---
    # Jobsites table uses these Airtable field names:
    JOBSITES_NAME_FIELD: str = os.getenv("JOBSITES_NAME_FIELD", "Name")
    JOBSITES_ADDRESS_FIELD: str = os.getenv("JOBSITES_ADDRESS_FIELD", "Address")

    # Deliveries table linked-record field that points to Jobsites (default "Job")
    # If your Deliveries table uses "job" or "Jobsite", set DELIVERIES_JOB_LINK_FIELD accordingly.
    DELIVERIES_JOB_LINK_FIELD: str = os.getenv("DELIVERIES_JOB_LINK_FIELD", "Job ID")
    DELIVERIES_SALES_ORDER_FIELD: str = os.getenv("DELIVERIES_SALES_ORDER_FIELD", "Sales Order")
    DELIVERIES_PS_DELIVERED_FIELD: str = os.getenv("DELIVERIES_PS_DELIVERED_FIELD", "PS Delivered")
    DELIVERIES_PACKING_SLIP_FIELD: str = os.getenv("DELIVERIES_PACKING_SLIP_FIELD", "Packing Slip")
    DELIVERIES_DRIVER_FIELD: str = os.getenv("DELIVERIES_DRIVER_FIELD", "Drivers")
    DELIVERIES_STATUS_FIELD: str = os.getenv("DELIVERIES_STATUS_FIELD", "Status")
    DELIVERIES_STATUS_UPDATED_FIELD: str = os.getenv("DELIVERIES_STATUS_UPDATED_FIELD", "Status Updated At")
    DELIVERIES_DRIVER_NOTES_FIELD: str = os.getenv("DELIVERIES_DRIVER_NOTES_FIELD", "Delivery Notes")
    DELIVERIES_PICKUP_VEHICLE_FIELD: str = os.getenv("DELIVERIES_PICKUP_VEHICLE_FIELD", "Pickup Vehicle")
    DELIVERIES_PHOTO_URLS_FIELD: str = os.getenv("DELIVERIES_PHOTO_URLS_FIELD", "")
    DELIVERIES_PHOTO_ATTACHMENTS: bool = _env_bool("DELIVERIES_PHOTO_ATTACHMENTS", "false")
    DELIVERIES_ARRIVED_AT_FIELD: str = os.getenv("DELIVERIES_ARRIVED_AT_FIELD", "Arrived At")
    DELIVERIES_DELIVERED_AT_FIELD: str = os.getenv("DELIVERIES_DELIVERED_AT_FIELD", "Delivered At")
    DELIVERIES_DRIVER_LAT_FIELD: str = os.getenv("DELIVERIES_DRIVER_LAT_FIELD", "Driver Latitude")
    DELIVERIES_DRIVER_LON_FIELD: str = os.getenv("DELIVERIES_DRIVER_LON_FIELD", "Driver Longitude")

    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "uploads")
    UPLOAD_MAX_MB: int = _env_int("UPLOAD_MAX_MB", "10")

    # Optional: where coordinates live in Jobsites (if you use them)
    JOBSITES_LAT_FIELD: str = os.getenv("JOBSITES_LAT_FIELD", "Latitude")
    JOBSITES_LON_FIELD: str = os.getenv("JOBSITES_LON_FIELD", "Longitude")

    # --- Auth (Airtable users) ---
    USERS_TABLE: str = os.getenv("USERS_TABLE", "Users")
    USERS_EMAIL_FIELD: str = os.getenv("USERS_EMAIL_FIELD", "Email")
    USERS_PASSWORD_FIELD: str = os.getenv("USERS_PASSWORD_FIELD", "Password Hash")
    USERS_ROLE_FIELD: str = os.getenv("USERS_ROLE_FIELD", "Role")
    USERS_ACTIVE_FIELD: str = os.getenv("USERS_ACTIVE_FIELD", "Active")
    USERS_NAME_FIELD: str = os.getenv("USERS_NAME_FIELD", "Name")
    USERS_SESSION_TOKEN_FIELD: str = os.getenv("USERS_SESSION_TOKEN_FIELD", "Session Token")
    USERS_SESSION_EXPIRES_FIELD: str = os.getenv("USERS_SESSION_EXPIRES_FIELD", "Session Expires")
    DRIVERS_ROLE_VALUE: str = os.getenv("DRIVERS_ROLE_VALUE", "driver")

    # --- Published plans (Airtable) ---
    PUBLISHED_PLANS_TABLE: str = os.getenv("PUBLISHED_PLANS_TABLE", "Published Plans")
    PUBLISHED_PLANS_DATE_FIELD: str = os.getenv("PUBLISHED_PLANS_DATE_FIELD", "Plan Date")
    PUBLISHED_PLANS_FLEET_FIELD: str = os.getenv("PUBLISHED_PLANS_FLEET_FIELD", "Fleet JSON")
    PUBLISHED_PLANS_ROUTES_FIELD: str = os.getenv("PUBLISHED_PLANS_ROUTES_FIELD", "Routes JSON")
    PUBLISHED_PLANS_DELIVERIES_FIELD: str = os.getenv("PUBLISHED_PLANS_DELIVERIES_FIELD", "Deliveries JSON")
    PUBLISHED_PLANS_META_FIELD: str = os.getenv("PUBLISHED_PLANS_META_FIELD", "Meta JSON")
    PUBLISHED_PLANS_DRIVER_PROFILES_FIELD: str = os.getenv("PUBLISHED_PLANS_DRIVER_PROFILES_FIELD", "Driver Profiles JSON")
    PUBLISHED_PLANS_STATUS_FIELD: str = os.getenv("PUBLISHED_PLANS_STATUS_FIELD", "Status")
    PUBLISHED_PLANS_PUBLISHED_BY_FIELD: str = os.getenv("PUBLISHED_PLANS_PUBLISHED_BY_FIELD", "Published By")
    PUBLISHED_PLANS_UPDATED_AT_FIELD: str = os.getenv("PUBLISHED_PLANS_UPDATED_AT_FIELD", "Published At")
    PUBLISHED_PLANS_TYPE_FIELD: str = os.getenv("PUBLISHED_PLANS_TYPE_FIELD", "Plan Type")


settings = Settings()
