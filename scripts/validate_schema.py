import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings


EXPECTED_TABLES = {
    "Deliveries": {
        "Delivery ID": {"types": {"integer"}},
        "Job ID": {"types": {"integer"}},
        "Sales Order": {"types": {"integer"}},
        "PS Delivered": {"types": {"boolean"}},
        "Scheduled Date": {"types": {"date"}},
        "Med Skid": {"types": {"integer"}},
        "Small Skid": {"types": {"integer"}},
        "Large Skid": {"types": {"integer"}},
        "Pickup": {"types": {"boolean"}},
        "Pickup Vehicle": {"types": {"text", "character varying"}},
        "Requires Empty Truck": {"types": {"boolean"}},
        "Door": {"types": {"integer"}},
        "Frame": {"types": {"integer"}},
        "Box": {"types": {"integer"}},
        "Drivers": {"types": {"text", "character varying"}},
        "Delivery Notes": {"types": {"text", "character varying"}},
        "Window Start": {"types": {"text", "character varying"}},
        "Window End": {"types": {"text", "character varying"}},
        "Packing Slip": {"types": {"integer"}},
    },
    "Jobsites": {
        "Job ID": {"types": {"integer"}},
        "Name": {"types": {"text", "character varying"}},
        "Address": {"types": {"text", "character varying"}},
        "Latitude": {"types": {"double precision", "real", "numeric"}},
        "Longitude": {"types": {"double precision", "real", "numeric"}},
        "Site Contact": {"types": {"text", "character varying"}},
        "Site Contact Number": {"types": {"text", "character varying"}},
    },
    "Published Plans": {
        "Plan Date": {"types": {"date"}},
        "Fleet JSON": {"types": {"text", "character varying"}},
        "Routes JSON": {"types": {"text", "character varying"}},
        "Deliveries JSON": {"types": {"text", "character varying"}},
        "Meta JSON": {"types": {"text", "character varying"}},
        "Driver Profiles JSON": {"types": {"text", "character varying"}},
        "Status": {"types": {"text", "character varying"}},
        "Published By": {"types": {"text", "character varying"}},
        "Published At": {"types": {"text", "character varying"}},
    },
    "Users": {
        "Email": {"types": {"text", "character varying"}},
        "Password Hash": {"types": {"text", "character varying"}},
        "Role": {"types": {"text", "character varying"}},
        "Active": {"types": {"boolean"}},
        "Name": {"types": {"text", "character varying"}},
        "Session Token": {"types": {"text", "character varying"}},
        "Session Expires": {"types": {"text", "character varying"}},
    },
}

EXPECTED_FKS = {
    ("Deliveries", "Job ID", "Jobsites", "Job ID"),
    ("Published Plans", "Published By", "Users", "Email"),
}


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

    if issues:
        print("Schema validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("Schema validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
