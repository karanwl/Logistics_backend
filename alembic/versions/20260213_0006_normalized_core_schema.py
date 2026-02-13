"""Create normalized core schema and live views.

Revision ID: 20260213_0006
Revises: 20260213_0005
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260213_0006"
down_revision = "20260213_0005"
branch_labels = None
depends_on = None


def _table_names(inspector) -> set[str]:
    return set(inspector.get_table_names())


def _has_table(inspector, table_name: str) -> bool:
    return table_name in _table_names(inspector)


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return any(idx.get("name") == index_name for idx in inspector.get_indexes(table_name))


def _has_fk(inspector, table_name: str, constrained_columns: list[str], referred_table: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    wanted_cols = list(constrained_columns)
    for fk in inspector.get_foreign_keys(table_name):
        if list(fk.get("constrained_columns") or []) == wanted_cols and fk.get("referred_table") == referred_table:
            return True
    return False


def _index_name_exists(bind, index_name: str) -> bool:
    value = bind.execute(sa.text("SELECT to_regclass(:idx_name)"), {"idx_name": f"public.{index_name}"}).scalar()
    return value is not None


def _legacy_col_expr(columns: set[str], col_name: str, default_sql: str = "NULL") -> str:
    if col_name in columns:
        return f'd."{col_name}"'
    return default_sql


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Timestamp parser used by migration copy and by compatibility queries.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.try_parse_timestamptz(value text)
        RETURNS timestamptz
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parsed timestamptz;
        BEGIN
            IF value IS NULL OR btrim(value) = '' THEN
                RETURN NULL;
            END IF;
            BEGIN
                parsed := value::timestamptz;
                RETURN parsed;
            EXCEPTION WHEN others THEN
                RETURN NULL;
            END;
        END;
        $$;
        """
    )

    if not _has_table(inspector, "users"):
        op.create_table(
            "users",
            sa.Column("email", sa.Text(), primary_key=True),
            sa.Column("password_hash", sa.Text(), nullable=True),
            sa.Column("role", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("session_token", sa.Text(), nullable=True),
            sa.Column("session_expires", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "jobsites"):
        op.create_table(
            "jobsites",
            sa.Column("id", sa.Integer(), sa.Identity(start=1), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            sa.Column("site_contact", sa.Text(), nullable=True),
            sa.Column("site_contact_number", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "deliveries"):
        op.create_table(
            "deliveries",
            sa.Column("id", sa.Integer(), sa.Identity(start=1), primary_key=True),
            sa.Column("jobsite_id", sa.Integer(), nullable=True),
            sa.Column("sales_order", sa.Integer(), nullable=True),
            sa.Column("ps_delivered", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("scheduled_date", sa.Date(), nullable=True),
            sa.Column("med_skid", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("small_skid", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("large_skid", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("pickup", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("pickup_vehicle", sa.Text(), nullable=True),
            sa.Column("requires_empty_truck", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("door", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("frame", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("exp_frame", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("box", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("driver_name", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=True),
            sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("driver_latitude", sa.Float(), nullable=True),
            sa.Column("driver_longitude", sa.Float(), nullable=True),
            sa.Column("driver_notes", sa.Text(), nullable=True),
            sa.Column("window_start", sa.Text(), nullable=True),
            sa.Column("window_end", sa.Text(), nullable=True),
            sa.Column("packing_slip", sa.Integer(), nullable=True),
            sa.Column("route_sequence", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "published_plans"):
        op.create_table(
            "published_plans",
            sa.Column("plan_date", sa.Date(), nullable=False),
            sa.Column("plan_type", sa.Text(), nullable=False, server_default=sa.text("'published'")),
            sa.Column("fleet_json", sa.Text(), nullable=True),
            sa.Column("routes_json", sa.Text(), nullable=True),
            sa.Column("deliveries_json", sa.Text(), nullable=True),
            sa.Column("meta_json", sa.Text(), nullable=True),
            sa.Column("driver_profiles_json", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=True),
            sa.Column("published_by", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.PrimaryKeyConstraint("plan_date", "plan_type", name="published_plans_pkey"),
        )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "Users") and _has_table(inspector, "users"):
        op.execute(
            """
            INSERT INTO users (
                email,
                password_hash,
                role,
                active,
                name,
                session_token,
                session_expires
            )
            SELECT
                lower(btrim(u."Email")),
                u."Password Hash",
                u."Role",
                COALESCE(u."Active", TRUE),
                u."Name",
                NULLIF(btrim(u."Session Token"), ''),
                public.try_parse_timestamptz(u."Session Expires")
            FROM "Users" u
            WHERE u."Email" IS NOT NULL AND btrim(u."Email") <> ''
            ON CONFLICT (email) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                role = EXCLUDED.role,
                active = EXCLUDED.active,
                name = EXCLUDED.name,
                session_token = EXCLUDED.session_token,
                session_expires = EXCLUDED.session_expires
            """
        )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "Jobsites") and _has_table(inspector, "jobsites"):
        op.execute(
            """
            INSERT INTO jobsites (
                id,
                name,
                address,
                latitude,
                longitude,
                site_contact,
                site_contact_number
            )
            SELECT
                j."Job ID",
                j."Name",
                j."Address",
                j."Latitude",
                j."Longitude",
                j."Site Contact",
                j."Site Contact Number"
            FROM "Jobsites" j
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                address = EXCLUDED.address,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                site_contact = EXCLUDED.site_contact,
                site_contact_number = EXCLUDED.site_contact_number
            """
        )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "Deliveries") and _has_table(inspector, "deliveries"):
        legacy_cols = {col["name"] for col in inspector.get_columns("Deliveries")}
        med_expr = (
            'COALESCE(d."Med Skid", d."Skid", 0)'
            if ("Med Skid" in legacy_cols and "Skid" in legacy_cols)
            else (
                'COALESCE(d."Med Skid", 0)'
                if "Med Skid" in legacy_cols
                else ('COALESCE(d."Skid", 0)' if "Skid" in legacy_cols else "0")
            )
        )
        route_seq_expr = _legacy_col_expr(legacy_cols, "Route Sequence", "NULL")
        op.execute(
            f"""
            INSERT INTO deliveries (
                id,
                jobsite_id,
                sales_order,
                ps_delivered,
                scheduled_date,
                med_skid,
                small_skid,
                large_skid,
                pickup,
                pickup_vehicle,
                requires_empty_truck,
                door,
                frame,
                exp_frame,
                box,
                driver_name,
                status,
                status_updated_at,
                arrived_at,
                delivered_at,
                driver_latitude,
                driver_longitude,
                driver_notes,
                window_start,
                window_end,
                packing_slip,
                route_sequence
            )
            SELECT
                d."Delivery ID",
                {_legacy_col_expr(legacy_cols, "Job ID", "NULL")},
                {_legacy_col_expr(legacy_cols, "Sales Order", "NULL")},
                COALESCE({_legacy_col_expr(legacy_cols, "PS Delivered", "FALSE")}, FALSE),
                {_legacy_col_expr(legacy_cols, "Scheduled Date", "NULL")},
                {med_expr},
                COALESCE({_legacy_col_expr(legacy_cols, "Small Skid", "0")}, 0),
                COALESCE({_legacy_col_expr(legacy_cols, "Large Skid", "0")}, 0),
                COALESCE({_legacy_col_expr(legacy_cols, "Pickup", "FALSE")}, FALSE),
                {_legacy_col_expr(legacy_cols, "Pickup Vehicle", "NULL")},
                COALESCE({_legacy_col_expr(legacy_cols, "Requires Empty Truck", "FALSE")}, FALSE),
                COALESCE({_legacy_col_expr(legacy_cols, "Door", "0")}, 0),
                COALESCE({_legacy_col_expr(legacy_cols, "Frame", "0")}, 0),
                COALESCE({_legacy_col_expr(legacy_cols, "Exp Frame", "0")}, 0),
                COALESCE({_legacy_col_expr(legacy_cols, "Box", "0")}, 0),
                {_legacy_col_expr(legacy_cols, "Drivers", "NULL")},
                {_legacy_col_expr(legacy_cols, "Status", "NULL")},
                public.try_parse_timestamptz({_legacy_col_expr(legacy_cols, "Status Updated At", "NULL")}),
                public.try_parse_timestamptz({_legacy_col_expr(legacy_cols, "Arrived At", "NULL")}),
                public.try_parse_timestamptz({_legacy_col_expr(legacy_cols, "Delivered At", "NULL")}),
                {_legacy_col_expr(legacy_cols, "Driver Latitude", "NULL")},
                {_legacy_col_expr(legacy_cols, "Driver Longitude", "NULL")},
                {_legacy_col_expr(legacy_cols, "Delivery Notes", "NULL")},
                {_legacy_col_expr(legacy_cols, "Window Start", "NULL")},
                {_legacy_col_expr(legacy_cols, "Window End", "NULL")},
                {_legacy_col_expr(legacy_cols, "Packing Slip", "NULL")},
                {route_seq_expr}
            FROM "Deliveries" d
            ON CONFLICT (id) DO UPDATE SET
                jobsite_id = EXCLUDED.jobsite_id,
                sales_order = EXCLUDED.sales_order,
                ps_delivered = EXCLUDED.ps_delivered,
                scheduled_date = EXCLUDED.scheduled_date,
                med_skid = EXCLUDED.med_skid,
                small_skid = EXCLUDED.small_skid,
                large_skid = EXCLUDED.large_skid,
                pickup = EXCLUDED.pickup,
                pickup_vehicle = EXCLUDED.pickup_vehicle,
                requires_empty_truck = EXCLUDED.requires_empty_truck,
                door = EXCLUDED.door,
                frame = EXCLUDED.frame,
                exp_frame = EXCLUDED.exp_frame,
                box = EXCLUDED.box,
                driver_name = EXCLUDED.driver_name,
                status = EXCLUDED.status,
                status_updated_at = EXCLUDED.status_updated_at,
                arrived_at = EXCLUDED.arrived_at,
                delivered_at = EXCLUDED.delivered_at,
                driver_latitude = EXCLUDED.driver_latitude,
                driver_longitude = EXCLUDED.driver_longitude,
                driver_notes = EXCLUDED.driver_notes,
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                packing_slip = EXCLUDED.packing_slip,
                route_sequence = EXCLUDED.route_sequence
            """
        )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "Published Plans") and _has_table(inspector, "published_plans"):
        plan_cols = {col["name"] for col in inspector.get_columns("Published Plans")}
        plan_type_expr = (
            "COALESCE(NULLIF(btrim(p.\"Plan Type\"), ''), 'published')"
            if "Plan Type" in plan_cols
            else "'published'"
        )
        op.execute(
            f"""
            INSERT INTO published_plans (
                plan_date,
                plan_type,
                fleet_json,
                routes_json,
                deliveries_json,
                meta_json,
                driver_profiles_json,
                status,
                published_by,
                published_at
            )
            SELECT
                p."Plan Date",
                {plan_type_expr},
                p."Fleet JSON",
                p."Routes JSON",
                p."Deliveries JSON",
                p."Meta JSON",
                p."Driver Profiles JSON",
                p."Status",
                NULLIF(btrim(p."Published By"), ''),
                public.try_parse_timestamptz(p."Published At")
            FROM "Published Plans" p
            ON CONFLICT (plan_date, plan_type) DO UPDATE SET
                fleet_json = EXCLUDED.fleet_json,
                routes_json = EXCLUDED.routes_json,
                deliveries_json = EXCLUDED.deliveries_json,
                meta_json = EXCLUDED.meta_json,
                driver_profiles_json = EXCLUDED.driver_profiles_json,
                status = EXCLUDED.status,
                published_by = EXCLUDED.published_by,
                published_at = EXCLUDED.published_at
            """
        )

    # Align identity sequences after backfill.
    if _has_table(sa.inspect(bind), "jobsites"):
        op.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('jobsites', 'id'),
                COALESCE((SELECT MAX(id) FROM jobsites), 1),
                TRUE
            )
            """
        )
    if _has_table(sa.inspect(bind), "deliveries"):
        op.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('deliveries', 'id'),
                COALESCE((SELECT MAX(id) FROM deliveries), 1),
                TRUE
            )
            """
        )

    inspector = sa.inspect(bind)
    # Clean invalid references before enabling FK.
    if _has_table(inspector, "deliveries") and _has_table(inspector, "jobsites"):
        op.execute(
            """
            UPDATE deliveries d
            SET jobsite_id = NULL
            WHERE d.jobsite_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM jobsites j WHERE j.id = d.jobsite_id
              )
            """
        )
        inspector = sa.inspect(bind)
        if not _has_fk(inspector, "deliveries", ["jobsite_id"], "jobsites"):
            op.create_foreign_key(
                "fk_deliveries_jobsite_id",
                source_table="deliveries",
                referent_table="jobsites",
                local_cols=["jobsite_id"],
                remote_cols=["id"],
                ondelete="SET NULL",
            )

    inspector = sa.inspect(bind)
    if _has_table(inspector, "deliveries"):
        if not _has_index(inspector, "deliveries", "ix_deliveries_scheduled_date") and not _index_name_exists(bind, "ix_deliveries_scheduled_date"):
            op.create_index("ix_deliveries_scheduled_date", "deliveries", ["scheduled_date"])
        if not _has_index(inspector, "deliveries", "ix_deliveries_driver") and not _index_name_exists(bind, "ix_deliveries_driver"):
            op.create_index("ix_deliveries_driver", "deliveries", ["driver_name"])
        if not _has_index(inspector, "deliveries", "ix_deliveries_job_id") and not _index_name_exists(bind, "ix_deliveries_job_id"):
            op.create_index("ix_deliveries_job_id", "deliveries", ["jobsite_id"])
        if not _has_index(inspector, "deliveries", "ix_deliveries_route_sequence") and not _index_name_exists(bind, "ix_deliveries_route_sequence"):
            op.create_index("ix_deliveries_route_sequence", "deliveries", ["route_sequence"])
        if not _has_index(inspector, "deliveries", "ix_deliveries_scheduled_driver_sequence") and not _index_name_exists(bind, "ix_deliveries_scheduled_driver_sequence"):
            op.create_index(
                "ix_deliveries_scheduled_driver_sequence",
                "deliveries",
                ["scheduled_date", "driver_name", "route_sequence"],
            )
        if not _has_index(inspector, "deliveries", "ix_deliveries_status") and not _index_name_exists(bind, "ix_deliveries_status"):
            op.create_index("ix_deliveries_status", "deliveries", ["status"])
        if not _has_index(inspector, "deliveries", "ix_deliveries_status_updated_at") and not _index_name_exists(bind, "ix_deliveries_status_updated_at"):
            op.create_index("ix_deliveries_status_updated_at", "deliveries", ["status_updated_at"])

    op.execute(
        """
        CREATE OR REPLACE VIEW public.v_route_live AS
        WITH base AS (
            SELECT
                d.scheduled_date AS live_date,
                d.id::text AS delivery_id,
                d.jobsite_id::text AS jobsite_id,
                COALESCE(NULLIF(btrim(d.driver_name), ''), 'Unassigned') AS driver_name,
                d.route_sequence AS route_sequence,
                COALESCE(NULLIF(btrim(j.name), ''), 'Delivery ' || d.id::text) AS job_name,
                COALESCE(NULLIF(btrim(j.address), ''), '') AS job_address,
                COALESCE(d.sales_order::text, '') AS sales_order,
                COALESCE(d.packing_slip::text, '') AS packing_slip,
                lower(replace(replace(COALESCE(NULLIF(btrim(d.status), ''), ''), '-', '_'), ' ', '_')) AS status_norm,
                COALESCE(d.ps_delivered, FALSE) AS ps_delivered,
                d.status_updated_at AS status_updated_at,
                d.arrived_at AS arrived_at,
                d.delivered_at AS delivered_at,
                d.driver_latitude AS driver_latitude,
                d.driver_longitude AS driver_longitude,
                COALESCE(d.med_skid, 0) AS med_skid,
                COALESCE(d.small_skid, 0) AS small_skid,
                COALESCE(d.large_skid, 0) AS large_skid,
                COALESCE(d.door, 0) AS door,
                COALESCE(d.frame, 0) AS frame,
                COALESCE(d.exp_frame, 0) AS exp_frame,
                COALESCE(d.box, 0) AS box,
                COALESCE(d.pickup, FALSE) AS pickup,
                COALESCE(d.pickup_vehicle, '') AS pickup_vehicle,
                COALESCE(d.requires_empty_truck, FALSE) AS requires_empty_truck,
                COALESCE(d.window_start, '') AS window_start,
                COALESCE(d.window_end, '') AS window_end,
                COALESCE(d.driver_notes, '') AS delivery_notes
            FROM deliveries d
            LEFT JOIN jobsites j ON j.id = d.jobsite_id
        )
        SELECT
            live_date,
            ('driver:' || regexp_replace(lower(driver_name), '[^a-z0-9]+', '-', 'g')) AS route_key,
            driver_name,
            delivery_id,
            jobsite_id,
            job_name,
            job_address,
            sales_order,
            packing_slip,
            route_sequence,
            CASE
                WHEN status_norm = 'failed' THEN 'failed'
                WHEN status_norm = 'delivered' OR delivered_at IS NOT NULL OR ps_delivered THEN 'delivered'
                WHEN status_norm IN ('arrived', 'en_route', 'in_progress') THEN status_norm
                ELSE 'pending'
            END AS status_code,
            CASE
                WHEN status_norm = 'failed' THEN 'failed'
                WHEN status_norm = 'delivered' OR delivered_at IS NOT NULL OR ps_delivered THEN 'done'
                WHEN status_norm IN ('arrived', 'en_route', 'in_progress') THEN 'current'
                ELSE 'upcoming'
            END AS progress_state,
            CASE
                WHEN status_norm = 'failed' THEN 'Failed'
                WHEN status_norm = 'delivered' OR delivered_at IS NOT NULL OR ps_delivered THEN 'Delivered'
                WHEN status_norm = 'arrived' THEN 'Arrived'
                WHEN status_norm IN ('en_route', 'in_progress') THEN 'En Route'
                ELSE 'Upcoming'
            END AS status_label,
            CASE
                WHEN status_norm = 'delivered' OR delivered_at IS NOT NULL OR ps_delivered
                    THEN COALESCE(delivered_at, status_updated_at, arrived_at)
                WHEN status_norm = 'failed'
                    THEN COALESCE(status_updated_at, arrived_at)
                WHEN status_norm IN ('arrived', 'en_route', 'in_progress')
                    THEN COALESCE(status_updated_at, arrived_at)
                ELSE NULL
            END AS status_time,
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
        FROM base;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW public.v_driver_progress_live AS
        SELECT
            live_date,
            route_key,
            driver_name,
            COUNT(*)::int AS total_stops,
            COUNT(*) FILTER (WHERE progress_state = 'done')::int AS done_stops,
            COUNT(*) FILTER (WHERE progress_state = 'current')::int AS current_stops,
            COUNT(*) FILTER (WHERE progress_state = 'upcoming')::int AS upcoming_stops,
            COUNT(*) FILTER (WHERE progress_state = 'failed')::int AS failed_stops,
            MAX(delivered_at) AS last_completed_at,
            MAX(status_updated_at) AS last_status_update_at
        FROM public.v_route_live
        GROUP BY live_date, route_key, driver_name;
        """
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute('DROP VIEW IF EXISTS public.v_driver_progress_live')
    op.execute('DROP VIEW IF EXISTS public.v_route_live')
    op.execute('DROP FUNCTION IF EXISTS public.try_parse_timestamptz(text)')

    inspector = sa.inspect(bind)
    if _has_table(inspector, "deliveries"):
        for index_name in [
            "ix_deliveries_status_updated_at",
            "ix_deliveries_status",
            "ix_deliveries_scheduled_driver_sequence",
            "ix_deliveries_route_sequence",
            "ix_deliveries_job_id",
            "ix_deliveries_driver",
            "ix_deliveries_scheduled_date",
        ]:
            if _has_index(inspector, "deliveries", index_name):
                op.drop_index(index_name, table_name="deliveries")
        inspector = sa.inspect(bind)
        if _has_fk(inspector, "deliveries", ["jobsite_id"], "jobsites"):
            op.drop_constraint("fk_deliveries_jobsite_id", "deliveries", type_="foreignkey")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "published_plans"):
        op.drop_table("published_plans")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "deliveries"):
        op.drop_table("deliveries")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "jobsites"):
        op.drop_table("jobsites")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "users"):
        op.drop_table("users")
