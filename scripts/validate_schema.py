import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings


EXPECTED_TABLES = {
    "users": {
        "email": {"types": {"text", "character varying"}},
        "password_hash": {"types": {"text", "character varying"}},
        "role": {"types": {"text", "character varying"}},
        "active": {"types": {"boolean"}},
        "name": {"types": {"text", "character varying"}},
        "session_token": {"types": {"text", "character varying"}},
        "session_expires": {"types": {"timestamp with time zone"}},
    },
    "jobsites": {
        "id": {"types": {"integer"}},
        "name": {"types": {"text", "character varying"}},
        "address": {"types": {"text", "character varying"}},
        "latitude": {"types": {"double precision", "real", "numeric"}},
        "longitude": {"types": {"double precision", "real", "numeric"}},
        "site_contact": {"types": {"text", "character varying"}},
        "site_contact_number": {"types": {"text", "character varying"}},
    },
    "deliveries": {
        "id": {"types": {"integer"}},
        "jobsite_id": {"types": {"integer"}},
        "sales_order": {"types": {"integer"}},
        "ps_delivered": {"types": {"boolean"}},
        "scheduled_date": {"types": {"date"}},
        "med_skid": {"types": {"integer"}},
        "small_skid": {"types": {"integer"}},
        "large_skid": {"types": {"integer"}},
        "pickup": {"types": {"boolean"}},
        "pickup_vehicle": {"types": {"text", "character varying"}},
        "requires_empty_truck": {"types": {"boolean"}},
        "door": {"types": {"integer"}},
        "frame": {"types": {"integer"}},
        "exp_frame": {"types": {"integer"}},
        "box": {"types": {"integer"}},
        "driver_name": {"types": {"text", "character varying"}},
        "driver_notes": {"types": {"text", "character varying"}},
        "window_start": {"types": {"text", "character varying"}},
        "window_end": {"types": {"text", "character varying"}},
        "packing_slip": {"types": {"integer"}},
        "status": {"types": {"text", "character varying"}},
        "status_updated_at": {"types": {"timestamp with time zone"}},
        "arrived_at": {"types": {"timestamp with time zone"}},
        "delivered_at": {"types": {"timestamp with time zone"}},
        "driver_latitude": {"types": {"double precision", "real", "numeric"}},
        "driver_longitude": {"types": {"double precision", "real", "numeric"}},
        "route_sequence": {"types": {"integer"}},
    },
}

EXPECTED_FKS = {
    ("deliveries", "jobsite_id", "jobsites", "id"),
}

EXPECTED_VIEWS = {"v_route_live", "v_driver_progress_live"}


def main() -> int:
    if not settings.DATABASE_URL:
        print("Missing DATABASE_URL.", file=sys.stderr)
        return 2

    engine = create_engine(settings.DATABASE_URL)
    issues = []

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                """
            )
        ).mappings().all()

        columns_by_table = {}
        for row in rows:
            columns_by_table.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]

        for table, columns in EXPECTED_TABLES.items():
            actual_cols = columns_by_table.get(table)
            if actual_cols is None:
                issues.append(f"Missing table: {table}")
                continue

            for col_name, meta in columns.items():
                actual_type = actual_cols.get(col_name)
                if actual_type is None:
                    issues.append(f"Missing column: {table}.{col_name}")
                    continue
                if actual_type not in meta["types"]:
                    exp = ", ".join(sorted(meta["types"]))
                    issues.append(
                        f"Type mismatch: {table}.{col_name} expected {exp}, got {actual_type}"
                    )

        fk_rows = conn.execute(
            text(
                """
                SELECT
                    tc.table_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                """
            )
        ).mappings().all()

        actual_fks = {
            (row["table_name"], row["column_name"], row["foreign_table_name"], row["foreign_column_name"])
            for row in fk_rows
        }

        for fk in EXPECTED_FKS:
            if fk not in actual_fks:
                issues.append(
                    f"Missing foreign key: {fk[0]}.{fk[1]} -> {fk[2]}.{fk[3]}"
                )

        view_rows = conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.views
                WHERE table_schema = 'public'
                """
            )
        ).mappings().all()
        actual_views = {row["table_name"] for row in view_rows}
        for view_name in EXPECTED_VIEWS:
            if view_name not in actual_views:
                issues.append(f"Missing view: {view_name}")

    if issues:
        print("Schema validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("Schema validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
