"""Use plan type in Published Plans keys.

Revision ID: 20260203_0003
Revises: 20260203_0002
Create Date: 2026-02-03
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260203_0003"
down_revision = "20260203_0002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "Published Plans" not in table_names:
        return

    plan_cols = {col["name"] for col in inspector.get_columns("Published Plans")}
    if "Plan Type" not in plan_cols:
        op.add_column(
            "Published Plans",
            sa.Column("Plan Type", sa.Text(), nullable=True, server_default="published"),
        )

    op.execute('UPDATE "Published Plans" SET "Plan Type" = \'published\' WHERE "Plan Type" IS NULL')
    op.alter_column("Published Plans", "Plan Type", nullable=False, server_default=None)

    inspector = sa.inspect(bind)
    pk = inspector.get_pk_constraint("Published Plans") or {}
    pk_name = pk.get("name") or "Published Plans_pkey"
    pk_cols = set(pk.get("constrained_columns") or [])
    if pk_cols != {"Plan Date", "Plan Type"}:
        op.drop_constraint(pk_name, "Published Plans", type_="primary")
        op.create_primary_key("Published Plans_pkey", "Published Plans", ["Plan Date", "Plan Type"])

    # Migrate existing snapshot data from Saved Plans into Published Plans by type.
    if "Saved Plans" in table_names:
        op.execute(
            """
            INSERT INTO "Published Plans"
                ("Plan Date", "Plan Type", "Routes JSON", "Deliveries JSON", "Meta JSON", "Status", "Published By", "Published At")
            SELECT
                sp."Plan Date",
                lower(trim(sp."Plan Type")),
                sp."Routes JSON",
                sp."Deliveries JSON",
                sp."Meta JSON",
                CASE
                    WHEN lower(trim(sp."Plan Type")) = 'manual' THEN 'Manual Draft'
                    WHEN lower(trim(sp."Plan Type")) = 'auto' THEN 'Auto Draft'
                    ELSE NULL
                END,
                sp."Updated By",
                sp."Updated At"
            FROM "Saved Plans" sp
            WHERE sp."Plan Type" IS NOT NULL
            ON CONFLICT ("Plan Date", "Plan Type") DO UPDATE
            SET
                "Routes JSON" = EXCLUDED."Routes JSON",
                "Deliveries JSON" = EXCLUDED."Deliveries JSON",
                "Meta JSON" = EXCLUDED."Meta JSON",
                "Status" = EXCLUDED."Status",
                "Published By" = EXCLUDED."Published By",
                "Published At" = EXCLUDED."Published At"
            """
        )


def downgrade():
    op.drop_constraint("Published Plans_pkey", "Published Plans", type_="primary")
    op.create_primary_key("Published Plans_pkey", "Published Plans", ["Plan Date"])
    op.drop_column("Published Plans", "Plan Type")
