"""Add photo URL refs column to deliveries

Revision ID: 20260218_0007
Revises: 20260213_0006
Create Date: 2026-02-18
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260218_0007"
down_revision = "20260213_0006"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "deliveries") and not _has_column(inspector, "deliveries", "photo_urls"):
        op.add_column("deliveries", sa.Column("photo_urls", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "deliveries") and _has_column(inspector, "deliveries", "photo_urls"):
        op.drop_column("deliveries", "photo_urls")
