"""SQLAlchemy data model (SPEC §3).

Conventions:
- All datetimes are *naive local time* in the configured TZ (Europe/Prague).
  Manual readings are stored at 00:00 of the reading day.
- kWh values use the `KWh` type (Decimal, exact to 3 decimal places).
- String enums are stored as plain strings; allowed values are listed in the
  `*_VALUES` tuples and enforced by CHECK constraints.
"""

from __future__ import annotations

import datetime as dt
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, KWh

# --- enum value sets -------------------------------------------------------

POINT_ROLES = ("main", "sub", "control")
REGISTERS = ("T1", "T2")
SOURCE_TYPES = ("manual", "live")
READING_SOURCES = ("manual", "photo", "import", "live")
READING_STATUSES = ("confirmed", "pending")
USER_ROLES = ("admin", "reader")
EXTRACTION_STATUSES = ("queued", "done", "failed", "skipped")
PHOTO_REVIEW_STATUSES = ("new", "confirmed", "discarded")
SETTLEMENT_STATUSES = ("draft", "final")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _now() -> datetime:
    """Naive local time (the app's convention; SQLite CURRENT_TIMESTAMP would be UTC)."""
    return datetime.now().replace(microsecond=0)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, server_default=func.current_timestamp()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", USER_ROLES), name="ck_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(16), default="reader")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def display_name(self) -> str:
        return self.name or self.email


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_remainder: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stable chart colour (hex) for light and dark themes.
    color: Mapped[str | None] = mapped_column(String(16))
    color_dark: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")

    allocations: Mapped[list[Allocation]] = relationship(back_populates="tenant")


class MeterPoint(TimestampMixin, Base):
    __tablename__ = "meter_points"
    __table_args__ = (CheckConstraint(_in("role", POINT_ROLES), name="ck_meter_points_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")  # location / description
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # order on the walk screen
    role: Mapped[str] = mapped_column(String(16), default="sub")
    parent_point_id: Mapped[int | None] = mapped_column(ForeignKey("meter_points.id", ondelete="SET NULL"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")

    parent: Mapped[MeterPoint | None] = relationship(remote_side="MeterPoint.id", back_populates="children")
    children: Mapped[list[MeterPoint]] = relationship(back_populates="parent")
    meters: Mapped[list[Meter]] = relationship(back_populates="meter_point", order_by="Meter.id")
    allocations: Mapped[list[Allocation]] = relationship(back_populates="meter_point")


class Meter(TimestampMixin, Base):
    __tablename__ = "meters"
    __table_args__ = (
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_meters_source_type"),
        CheckConstraint("digits BETWEEN 1 AND 12", name="ck_meters_digits"),
        CheckConstraint("decimals_shown BETWEEN 0 AND 3", name="ck_meters_decimals_shown"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_point_id: Mapped[int] = mapped_column(ForeignKey("meter_points.id", ondelete="RESTRICT"), index=True)
    serial: Mapped[str] = mapped_column(String(100), unique=True)
    installed_on: Mapped[date | None] = mapped_column(Date)
    removed_on: Mapped[date | None] = mapped_column(Date)
    digits: Mapped[int] = mapped_column(Integer, default=5)  # integer places; counter rolls over at 10**digits
    decimals_shown: Mapped[int] = mapped_column(Integer, default=0)  # decimal places shown on the display
    # Free-text hint for reading/extraction, e.g. "displej cykluje T1/T2, OBIS 1.8.1 = T1".
    display_note: Mapped[str] = mapped_column(Text, default="")
    # Registers present on the meter, e.g. ["T1"] or ["T1", "T2"].
    registers: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["T1"])
    source_type: Mapped[str] = mapped_column(String(16), default="manual")
    source_config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")

    meter_point: Mapped[MeterPoint] = relationship(back_populates="meters")
    readings: Mapped[list[Reading]] = relationship(back_populates="meter", order_by="Reading.taken_at")

    @property
    def is_active(self) -> bool:
        return self.removed_on is None


class PointSkip(Base):
    """A meter point marked as inaccessible / skipped on a day, with a note (SPEC §5). [rev 0003]"""

    __tablename__ = "point_skips"
    __table_args__ = (UniqueConstraint("meter_point_id", "day", name="uq_point_skips_point_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_point_id: Mapped[int] = mapped_column(ForeignKey("meter_points.id", ondelete="CASCADE"), index=True)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, server_default=func.current_timestamp())

    meter_point: Mapped[MeterPoint] = relationship()


class Photo(TimestampMixin, Base):
    __tablename__ = "photos"
    __table_args__ = (
        CheckConstraint(_in("extraction_status", EXTRACTION_STATUSES), name="ck_photos_extraction_status"),
        CheckConstraint(_in("review_status", PHOTO_REVIEW_STATUSES), name="ck_photos_review_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True)
    # Paths relative to DATA_DIR, e.g. "photos/2025/01/<uuid>.jpg".
    path: Mapped[str] = mapped_column(String(500))
    thumb_path: Mapped[str | None] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500), default="")
    content_type: Mapped[str] = mapped_column(String(100), default="image/jpeg")
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    exif_taken_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Effective time of the photo (EXIF, else upload time; user-editable).
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    extraction_status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    extraction_attempts: Mapped[int] = mapped_column(Integer, default=0)
    extraction_model: Mapped[str | None] = mapped_column(String(200))
    extraction_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # raw extractor output
    extraction_error: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Photos taken within ~60 s of each other form a series (same group key).
    series_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Primary matched meter (after recognition or manual assignment) and how it was matched.
    # A photo may show several meters: readings link back via Reading.photo_id.
    meter_id: Mapped[int | None] = mapped_column(ForeignKey("meters.id", ondelete="SET NULL"), index=True)
    match_method: Mapped[str | None] = mapped_column(String(32))  # exact|fuzzy|values|manual
    review_status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # How the photo arrived: walk (walk PWA) | upload (batch) | share (Web Share Target). [rev 0002]
    source: Mapped[str] = mapped_column(String(16), default="upload", server_default="upload")
    # Who reported the value (e.g. a tenant sending photos via WhatsApp); copied to readings. [rev 0002]
    reported_by: Mapped[str] = mapped_column(String(200), default="", server_default="")

    meter: Mapped[Meter | None] = relationship()
    uploaded_by: Mapped[User | None] = relationship()


class Reading(TimestampMixin, Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("meter_id", "register", "taken_at", name="uq_readings_meter_register_time"),
        CheckConstraint(_in("register", REGISTERS), name="ck_readings_register"),
        CheckConstraint(_in("source", READING_SOURCES), name="ck_readings_source"),
        CheckConstraint(_in("status", READING_STATUSES), name="ck_readings_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id", ondelete="RESTRICT"), index=True)
    register: Mapped[str] = mapped_column(String(4), default="T1")
    # Real time the reading was taken (not entry time; late tenant reports use their own date).
    taken_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[Decimal] = mapped_column(KWh)  # as read, incl. decimals shown on the display
    source: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    photo_id: Mapped[int | None] = mapped_column(ForeignKey("photos.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reported_by: Mapped[str] = mapped_column(String(200), default="")  # e.g. tenant who sent the value
    note: Mapped[str] = mapped_column(Text, default="")
    # Idempotency key of an offline client submission ("<entry uuid>:<register>"). [rev 0002]
    client_ref: Mapped[str | None] = mapped_column(String(64), index=True, unique=True)

    meter: Mapped[Meter] = relationship(back_populates="readings")
    photo: Mapped[Photo | None] = relationship()
    created_by: Mapped[User | None] = relationship()


class Allocation(TimestampMixin, Base):
    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    meter_point_id: Mapped[int] = mapped_column(ForeignKey("meter_points.id", ondelete="CASCADE"), index=True)
    # Validity [valid_from, valid_to); None = unbounded.
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    factor: Mapped[Decimal] = mapped_column(KWh, default=Decimal(1))
    note: Mapped[str] = mapped_column(Text, default="")

    tenant: Mapped[Tenant] = relationship(back_populates="allocations")
    meter_point: Mapped[MeterPoint] = relationship(back_populates="allocations")


class Settlement(TimestampMixin, Base):
    __tablename__ = "settlements"
    __table_args__ = (CheckConstraint(_in("status", SETTLEMENT_STATUSES), name="ck_settlements_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    # Period [period_start, period_end) on aligned calendar months (start/end may be any dates). [rev 0003:
    # the old "rounds" boundary mode is gone; finalized snapshots keep their own "mode".]
    status: Mapped[str] = mapped_column(String(16), default="draft")
    # Frozen results at finalization (JSON), so later reading fixes don't change it.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    snapshot_version: Mapped[int | None] = mapped_column(Integer)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)
    finalized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Finalized despite provisional components (explicit confirmation + note required).
    finalize_forced: Mapped[bool] = mapped_column(Boolean, default=False)
    finalize_note: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Future: prices / invoice split (not used in MVP).
    pricing: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")

    finalized_by: Mapped[User | None] = relationship(foreign_keys=[finalized_by_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, server_default=func.current_timestamp(), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_email: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(32))  # create|update|delete|finalize|reopen|import|...
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # {"field": [old, new], ...} for updates; full values for create/delete.
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")
