from sqlalchemy import Boolean, Column, Date, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from db import Base


class Jobsite(Base):
    __tablename__ = "Jobsites"

    job_id = Column("Job ID", Integer, primary_key=True)
    name = Column("Name", Text, nullable=False)
    address = Column("Address", Text)
    latitude = Column("Latitude", Float)
    longitude = Column("Longitude", Float)
    site_contact = Column("Site Contact", Text)
    site_contact_number = Column("Site Contact Number", Text)

    deliveries = relationship("Delivery", back_populates="jobsite")


class Delivery(Base):
    __tablename__ = "Deliveries"

    delivery_id = Column("Delivery ID", Integer, primary_key=True)
    job_id = Column("Job ID", Integer, ForeignKey("Jobsites.Job ID"))
    sales_order = Column("Sales Order", Integer)
    ps_delivered = Column("PS Delivered", Boolean)
    scheduled_date = Column("Scheduled Date", Date)
    med_skid = Column("Med Skid", Integer)
    small_skid = Column("Small Skid", Integer)
    large_skid = Column("Large Skid", Integer)
    pickup = Column("Pickup", Boolean)
    pickup_vehicle = Column("Pickup Vehicle", Text)
    requires_empty_truck = Column("Requires Empty Truck", Boolean)
    door = Column("Door", Integer)
    frame = Column("Frame", Integer)
    exp_frame = Column("Exp Frame", Integer)
    box = Column("Box", Integer)
    drivers = Column("Drivers", Text)
    status = Column("Status", Text)
    status_updated_at = Column("Status Updated At", Text)
    arrived_at = Column("Arrived At", Text)
    delivered_at = Column("Delivered At", Text)
    driver_latitude = Column("Driver Latitude", Float)
    driver_longitude = Column("Driver Longitude", Float)
    delivery_notes = Column("Delivery Notes", Text)
    window_start = Column("Window Start", Text)
    window_end = Column("Window End", Text)
    packing_slip = Column("Packing Slip", Integer)

    jobsite = relationship("Jobsite", back_populates="deliveries")


class User(Base):
    __tablename__ = "Users"

    email = Column("Email", Text, primary_key=True)
    password_hash = Column("Password Hash", Text)
    role = Column("Role", Text)
    active = Column("Active", Boolean)
    name = Column("Name", Text)
    session_token = Column("Session Token", Text)
    session_expires = Column("Session Expires", Text)

    published_plans = relationship("PublishedPlan", back_populates="publisher")


class PublishedPlan(Base):
    __tablename__ = "Published Plans"

    plan_date = Column("Plan Date", Date, primary_key=True)
    plan_type = Column("Plan Type", Text, primary_key=True)
    fleet_json = Column("Fleet JSON", Text)
    routes_json = Column("Routes JSON", Text)
    deliveries_json = Column("Deliveries JSON", Text)
    meta_json = Column("Meta JSON", Text)
    driver_profiles_json = Column("Driver Profiles JSON", Text)
    status = Column("Status", Text)
    published_by = Column("Published By", Text, ForeignKey("Users.Email"))
    published_at = Column("Published At", Text)

    publisher = relationship("User", back_populates="published_plans")
