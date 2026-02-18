"""Add push token and idempotency persistence tables

Revision ID: 20260218_0008
Revises: 20260218_0007
Create Date: 2026-02-18
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260218_0008"
down_revision = "20260218_0007"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "push_tokens"):
        op.create_table(
            "push_tokens",
            sa.Column("identity", sa.Text(), nullable=False),
            sa.Column("token", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("identity", "token", name="push_tokens_pkey"),
        )
        op.create_index("ix_push_tokens_identity", "push_tokens", ["identity"], unique=False)
        op.create_index("ix_push_tokens_expires_at", "push_tokens", ["expires_at"], unique=False)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "idempotency_keys"):
        op.create_table(
            "idempotency_keys",
            sa.Column("idempotency_key", sa.Text(), nullable=False),
            sa.Column("request_method", sa.Text(), nullable=False),
            sa.Column("request_path", sa.Text(), nullable=False),
            sa.Column("request_hash", sa.Text(), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("idempotency_key", name="idempotency_keys_pkey"),
        )
        op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "idempotency_keys"):
        op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
        op.drop_table("idempotency_keys")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "push_tokens"):
        op.drop_index("ix_push_tokens_expires_at", table_name="push_tokens")
        op.drop_index("ix_push_tokens_identity", table_name="push_tokens")
        op.drop_table("push_tokens")
