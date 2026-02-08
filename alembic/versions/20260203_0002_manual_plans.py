"""Add manual routing storage and expand deliveries.

Revision ID: 20260203_0002
Revises: 20260201_0001
Create Date: 2026-02-03
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260203_0002"
down_revision = "20260201_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("Deliveries", sa.Column("Small Skid", sa.Integer(), nullable=True))
    op.add_column("Deliveries", sa.Column("Exp Frame", sa.Integer(), nullable=True))

    op.create_table(
        "Saved Plans",
        sa.Column("Plan Date", sa.Date(), primary_key=True),
        sa.Column("Plan Type", sa.Text(), primary_key=True),
        sa.Column("Routes JSON", sa.Text(), nullable=True),
        sa.Column("Deliveries JSON", sa.Text(), nullable=True),
        sa.Column("Meta JSON", sa.Text(), nullable=True),
        sa.Column("Updated At", sa.Text(), nullable=True),
        sa.Column("Updated By", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table("Saved Plans")
    op.drop_column("Deliveries", "Exp Frame")
    op.drop_column("Deliveries", "Small Skid")
