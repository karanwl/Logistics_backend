"""Initial schema

Revision ID: 20260201_0001
Revises: 
Create Date: 2026-02-01

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260201_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "Jobsites",
        sa.Column("Job ID", sa.Integer(), sa.Identity(start=1), primary_key=True),
        sa.Column("Name", sa.Text(), nullable=False),
        sa.Column("Address", sa.Text(), nullable=True),
        sa.Column("Latitude", sa.Float(), nullable=True),
        sa.Column("Longitude", sa.Float(), nullable=True),
        sa.Column("Site Contact", sa.Text(), nullable=True),
        sa.Column("Site Contact Number", sa.Text(), nullable=True),
    )

    op.create_table(
        "Users",
        sa.Column("Email", sa.Text(), primary_key=True),
        sa.Column("Password Hash", sa.Text(), nullable=True),
        sa.Column("Role", sa.Text(), nullable=True),
        sa.Column("Active", sa.Boolean(), nullable=True),
        sa.Column("Name", sa.Text(), nullable=True),
        sa.Column("Session Token", sa.Text(), nullable=True),
        sa.Column("Session Expires", sa.Text(), nullable=True),
    )

    op.create_table(
        "Published Plans",
        sa.Column("Plan Date", sa.Date(), primary_key=True),
        sa.Column("Fleet JSON", sa.Text(), nullable=True),
        sa.Column("Routes JSON", sa.Text(), nullable=True),
        sa.Column("Deliveries JSON", sa.Text(), nullable=True),
        sa.Column("Meta JSON", sa.Text(), nullable=True),
        sa.Column("Driver Profiles JSON", sa.Text(), nullable=True),
        sa.Column("Status", sa.Text(), nullable=True),
        sa.Column("Published By", sa.Text(), nullable=True),
        sa.Column("Published At", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["Published By"], ["Users.Email"]),
    )

    op.create_table(
        "Deliveries",
        sa.Column("Delivery ID", sa.Integer(), sa.Identity(start=1), primary_key=True),
        sa.Column("Job ID", sa.Integer(), nullable=True),
        sa.Column("Sales Order", sa.Integer(), nullable=True),
        sa.Column("PS Delivered", sa.Boolean(), nullable=True),
        sa.Column("Scheduled Date", sa.Date(), nullable=True),
        sa.Column("Skid", sa.Integer(), nullable=True),
        sa.Column("Door", sa.Integer(), nullable=True),
        sa.Column("Frame", sa.Integer(), nullable=True),
        sa.Column("Box", sa.Integer(), nullable=True),
        sa.Column("Drivers", sa.Text(), nullable=True),
        sa.Column("Delivery Notes", sa.Text(), nullable=True),
        sa.Column("Window Start", sa.Text(), nullable=True),
        sa.Column("Window End", sa.Text(), nullable=True),
        sa.Column("Packing Slip", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["Job ID"], ["Jobsites.Job ID"]),
    )


def downgrade():
    op.drop_table("Deliveries")
    op.drop_table("Published Plans")
    op.drop_table("Users")
    op.drop_table("Jobsites")
