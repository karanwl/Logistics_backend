from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from db import Base


class Jobsite(Base):
    __tablename__ = "jobsites"

    job_id = Column("jobsite_id", Integer, primary_key=True)
    name = Column("name", Text, nullable=False)
    address = Column("address_line1", Text)
    address_line2 = Column("address_line2", Text)
    city = Column("city", Text)
    state = Column("state", Text)
    postal_code = Column("postal_code", Text)
    country = Column("country", Text)
    latitude = Column("latitude", Float)
    longitude = Column("longitude", Float)
    site_contact = Column("site_contact_name", Text)
    site_contact_number = Column("site_contact_phone", Text)
    notes = Column("notes", Text)
    active = Column("active", Boolean)
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    deliveries = relationship("Delivery", back_populates="jobsite")


class Delivery(Base):
    __tablename__ = "deliveries"

    delivery_id = Column("delivery_id", Integer, primary_key=True)
    job_id = Column("jobsite_id", Integer, ForeignKey("jobsites.jobsite_id"))
    sales_order = Column("sales_order", Integer)
    packing_slip = Column("packing_slip", Integer)
    scheduled_date = Column("scheduled_date", Date)
    route_sequence = Column("route_sequence", Integer)
    assigned_driver_id = Column("assigned_driver_id", Integer, ForeignKey("users.user_id"))
    status = Column("status", Text)
    status_updated_at = Column("status_updated_at", DateTime(timezone=True))
    arrived_at = Column("arrived_at", DateTime(timezone=True))
    delivered_at = Column("delivered_at", DateTime(timezone=True))
    failed_at = Column("failed_at", DateTime(timezone=True))
    failure_reason = Column("failure_reason", Text)
    driver_notes = Column("driver_notes", Text)
    driver_latitude = Column("driver_latitude", Float)
    driver_longitude = Column("driver_longitude", Float)
    med_skid = Column("med_skid", Integer)
    small_skid = Column("small_skid", Integer)
    large_skid = Column("large_skid", Integer)
    door = Column("door", Integer)
    frame = Column("frame", Integer)
    exp_frame = Column("exp_frame", Integer)
    box = Column("box", Integer)
    pickup = Column("pickup", Boolean)
    pickup_vehicle = Column("pickup_vehicle", Text)
    requires_empty_truck = Column("requires_empty_truck", Boolean)
    window_start = Column("window_start", Text)
    window_end = Column("window_end", Text)
    delivery_notes = Column("delivery_notes", Text)
    custom_stop = Column("custom_stop", Boolean)
    custom_stop_label = Column("custom_stop_label", Text)
    created_at = Column("created_at", DateTime(timezone=True))
    updated_at = Column("updated_at", DateTime(timezone=True))

    jobsite = relationship("Jobsite", back_populates="deliveries")

    @property
    def drivers(self):
        return self.assigned_driver_id

    @drivers.setter
    def drivers(self, value):
        if value in (None, ""):
            self.assigned_driver_id = None
            return
        try:
            self.assigned_driver_id = int(value)
        except Exception:
            self.assigned_driver_id = None

    @property
    def ps_delivered(self):
        status = str(self.status or "").strip().lower()
        return bool(self.delivered_at) or status == "delivered"

    @ps_delivered.setter
    def ps_delivered(self, value):
        # Legacy compatibility: when older code toggles this flag, mirror it to status.
        if value:
            if not self.status:
                self.status = "delivered"
            return
        if str(self.status or "").strip().lower() == "delivered":
            self.status = None


class User(Base):
    __tablename__ = "users"

    user_id = Column("user_id", Integer, primary_key=True)
    email = Column("email", Text)
    password_hash = Column("password_hash", Text)
    full_name = Column("full_name", Text)
    role = Column("role", Text)
    active = Column("active", Boolean)
    last_login_at = Column("last_login_at", DateTime(timezone=True))
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
