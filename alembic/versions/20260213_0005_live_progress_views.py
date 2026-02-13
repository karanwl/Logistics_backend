"""Add live progress SQL views and routing indexes.

Revision ID: 20260213_0005
Revises: 20260213_0004
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260213_0005"
down_revision = "20260213_0004"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx.get("name") == index_name for idx in inspector.get_indexes(table_name))


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "Deliveries"

    if not _has_table(inspector, table_name):
        return

    if not _has_column(inspector, table_name, "Route Sequence"):
        op.add_column(table_name, sa.Column("Route Sequence", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if not _has_index(inspector, table_name, "ix_deliveries_scheduled_date"):
        op.create_index("ix_deliveries_scheduled_date", table_name, ["Scheduled Date"])
    if not _has_index(inspector, table_name, "ix_deliveries_driver"):
        op.create_index("ix_deliveries_driver", table_name, ["Drivers"])
    if not _has_index(inspector, table_name, "ix_deliveries_job_id"):
        op.create_index("ix_deliveries_job_id", table_name, ["Job ID"])
    if not _has_index(inspector, table_name, "ix_deliveries_route_sequence"):
        op.create_index("ix_deliveries_route_sequence", table_name, ["Route Sequence"])
    if not _has_index(inspector, table_name, "ix_deliveries_scheduled_driver_sequence"):
        op.create_index(
            "ix_deliveries_scheduled_driver_sequence",
            table_name,
            ["Scheduled Date", "Drivers", "Route Sequence"],
        )

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

    op.execute(
        """
        CREATE OR REPLACE VIEW public.v_route_live AS
        WITH base AS (
            SELECT
                d."Scheduled Date" AS live_date,
                d."Delivery ID"::text AS delivery_id,
                d."Job ID"::text AS jobsite_id,
                COALESCE(NULLIF(btrim(d."Drivers"), ''), 'Unassigned') AS driver_name,
                d."Route Sequence" AS route_sequence,
                COALESCE(NULLIF(btrim(j."Name"), ''), 'Delivery ' || d."Delivery ID"::text) AS job_name,
                COALESCE(NULLIF(btrim(j."Address"), ''), '') AS job_address,
                COALESCE(d."Sales Order"::text, '') AS sales_order,
                COALESCE(d."Packing Slip"::text, '') AS packing_slip,
                lower(replace(replace(COALESCE(NULLIF(btrim(d."Status"), ''), ''), '-', '_'), ' ', '_')) AS status_norm,
                COALESCE(d."PS Delivered", FALSE) AS ps_delivered,
                public.try_parse_timestamptz(d."Status Updated At") AS status_updated_at,
                public.try_parse_timestamptz(d."Arrived At") AS arrived_at,
                public.try_parse_timestamptz(d."Delivered At") AS delivered_at,
                d."Driver Latitude" AS driver_latitude,
                d."Driver Longitude" AS driver_longitude,
                COALESCE(d."Med Skid", 0) AS med_skid,
                COALESCE(d."Small Skid", 0) AS small_skid,
                COALESCE(d."Large Skid", 0) AS large_skid,
                COALESCE(d."Door", 0) AS door,
                COALESCE(d."Frame", 0) AS frame,
                COALESCE(d."Exp Frame", 0) AS exp_frame,
                COALESCE(d."Box", 0) AS box,
                COALESCE(d."Pickup", FALSE) AS pickup,
                COALESCE(d."Pickup Vehicle", '') AS pickup_vehicle,
                COALESCE(d."Requires Empty Truck", FALSE) AS requires_empty_truck,
                COALESCE(d."Window Start", '') AS window_start,
                COALESCE(d."Window End", '') AS window_end,
                COALESCE(d."Delivery Notes", '') AS delivery_notes
            FROM "Deliveries" d
            LEFT JOIN "Jobsites" j ON j."Job ID" = d."Job ID"
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
    table_name = "Deliveries"

    op.execute('DROP VIEW IF EXISTS public.v_driver_progress_live')
    op.execute('DROP VIEW IF EXISTS public.v_route_live')
    op.execute('DROP FUNCTION IF EXISTS public.try_parse_timestamptz(text)')

    if _has_table(inspector, table_name):
        if _has_index(inspector, table_name, "ix_deliveries_scheduled_driver_sequence"):
            op.drop_index("ix_deliveries_scheduled_driver_sequence", table_name=table_name)
        if _has_index(inspector, table_name, "ix_deliveries_route_sequence"):
            op.drop_index("ix_deliveries_route_sequence", table_name=table_name)
        if _has_index(inspector, table_name, "ix_deliveries_job_id"):
            op.drop_index("ix_deliveries_job_id", table_name=table_name)
        if _has_index(inspector, table_name, "ix_deliveries_driver"):
            op.drop_index("ix_deliveries_driver", table_name=table_name)
        if _has_index(inspector, table_name, "ix_deliveries_scheduled_date"):
            op.drop_index("ix_deliveries_scheduled_date", table_name=table_name)

        inspector = sa.inspect(bind)
        if _has_column(inspector, table_name, "Route Sequence"):
            op.drop_column(table_name, "Route Sequence")
