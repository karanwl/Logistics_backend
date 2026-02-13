from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from db import Base


class Jobsite(Base):
    __tablename__ = "jobsites"

    job_id = Column("id", Integer, primary_key=True)
    name = Column("name", Text, nullable=False)
    address = Column("address", Text)
    latitude = Column("latitude", Float)
    longitude = Column("longitude", Float)
    site_contact = Column("site_contact", Text)
    site_contact_number = Column("site_contact_number", Text)
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    deliveries = relationship("Delivery", back_populates="jobsite")


class Delivery(Base):
    __tablename__ = "deliveries"

    delivery_id = Column("id", Integer, primary_key=True)
    job_id = Column("jobsite_id", Integer, ForeignKey("jobsites.id"))
    sales_order = Column("sales_order", Integer)
    ps_delivered = Column("ps_delivered", Boolean)
    scheduled_date = Column("scheduled_date", Date)
    med_skid = Column("med_skid", Integer)
    small_skid = Column("small_skid", Integer)
    large_skid = Column("large_skid", Integer)
    pickup = Column("pickup", Boolean)
    pickup_vehicle = Column("pickup_vehicle", Text)
    requires_empty_truck = Column("requires_empty_truck", Boolean)
    door = Column("door", Integer)
    frame = Column("frame", Integer)
    exp_frame = Column("exp_frame", Integer)
    box = Column("box", Integer)
    drivers = Column("driver_name", Text)
    status = Column("status", Text)
    status_updated_at = Column("status_updated_at", DateTime(timezone=True))
    arrived_at = Column("arrived_at", DateTime(timezone=True))
    delivered_at = Column("delivered_at", DateTime(timezone=True))
    driver_latitude = Column("driver_latitude", Float)
    driver_longitude = Column("driver_longitude", Float)
    delivery_notes = Column("driver_notes", Text)
    window_start = Column("window_start", Text)
    window_end = Column("window_end", Text)
    packing_slip = Column("packing_slip", Integer)
    route_sequence = Column("route_sequence", Integer)
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    jobsite = relationship("Jobsite", back_populates="deliveries")


class User(Base):
    __tablename__ = "users"

    email = Column("email", Text, primary_key=True)
    password_hash = Column("password_hash", Text)
    role = Column("role", Text)
    active = Column("active", Boolean)
    name = Column("name", Text)
    session_token = Column("session_token", Text)
    session_expires = Column("session_expires", DateTime(timezone=True))
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    published_plans = relationship("PublishedPlan", back_populates="publisher")


class PublishedPlan(Base):
    __tablename__ = "published_plans"

    plan_date = Column("plan_date", Date, primary_key=True)
    plan_type = Column("plan_type", Text, primary_key=True)
    fleet_json = Column("fleet_json", Text)
    routes_json = Column("routes_json", Text)
    deliveries_json = Column("deliveries_json", Text)
    meta_json = Column("meta_json", Text)
    driver_profiles_json = Column("driver_profiles_json", Text)
    status = Column("status", Text)
    published_by = Column("published_by", Text, ForeignKey("users.email"))
    published_at = Column("published_at", DateTime(timezone=True))
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    publisher = relationship("User", back_populates="published_plans")
