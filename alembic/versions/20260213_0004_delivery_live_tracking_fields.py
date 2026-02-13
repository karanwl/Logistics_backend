"""Add bare-minimum delivery live tracking fields.

Revision ID: 20260213_0004
Revises: 20260203_0003
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260213_0004"
down_revision = "20260203_0003"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx.get("name") == index_name for idx in inspector.get_indexes(table_name))


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "Deliveries"
    if table_name not in set(inspector.get_table_names()):
        return

    if not _has_column(inspector, table_name, "Status"):
        op.add_column(table_name, sa.Column("Status", sa.Text(), nullable=True))
    if not _has_column(inspector, table_name, "Status Updated At"):
        op.add_column(table_name, sa.Column("Status Updated At", sa.Text(), nullable=True))
    if not _has_column(inspector, table_name, "Arrived At"):
        op.add_column(table_name, sa.Column("Arrived At", sa.Text(), nullable=True))
    if not _has_column(inspector, table_name, "Delivered At"):
        op.add_column(table_name, sa.Column("Delivered At", sa.Text(), nullable=True))
    if not _has_column(inspector, table_name, "Driver Latitude"):
        op.add_column(table_name, sa.Column("Driver Latitude", sa.Float(), nullable=True))
    if not _has_column(inspector, table_name, "Driver Longitude"):
        op.add_column(table_name, sa.Column("Driver Longitude", sa.Float(), nullable=True))

    inspector = sa.inspect(bind)
    if not _has_index(inspector, table_name, "ix_deliveries_status"):
        op.create_index("ix_deliveries_status", table_name, ["Status"])
    if not _has_index(inspector, table_name, "ix_deliveries_status_updated_at"):
        op.create_index("ix_deliveries_status_updated_at", table_name, ["Status Updated At"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "Deliveries"
    if table_name not in set(inspector.get_table_names()):
        return

    if _has_index(inspector, table_name, "ix_deliveries_status_updated_at"):
        op.drop_index("ix_deliveries_status_updated_at", table_name=table_name)
    if _has_index(inspector, table_name, "ix_deliveries_status"):
        op.drop_index("ix_deliveries_status", table_name=table_name)

    inspector = sa.inspect(bind)
    if _has_column(inspector, table_name, "Driver Longitude"):
        op.drop_column(table_name, "Driver Longitude")
    if _has_column(inspector, table_name, "Driver Latitude"):
        op.drop_column(table_name, "Driver Latitude")
    if _has_column(inspector, table_name, "Delivered At"):
        op.drop_column(table_name, "Delivered At")
    if _has_column(inspector, table_name, "Arrived At"):
        op.drop_column(table_name, "Arrived At")
    if _has_column(inspector, table_name, "Status Updated At"):
        op.drop_column(table_name, "Status Updated At")
    if _has_column(inspector, table_name, "Status"):
        op.drop_column(table_name, "Status")
