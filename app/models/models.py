from datetime import datetime, timezone
import uuid
from sqlalchemy import String, Float, Integer, Boolean, ForeignKey, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    inspectors: Mapped[list["Inspector"]] = relationship(back_populates="company")
    vehicles: Mapped[list["Vehicle"]] = relationship(back_populates="company")


class Inspector(Base):
    __tablename__ = "inspectors"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="inspector")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    company: Mapped["Company"] = relationship(back_populates="inspectors")
    inspections: Mapped[list["Inspection"]] = relationship(back_populates="inspector")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    plate: Mapped[str] = mapped_column(String, nullable=False, index=True)
    vin: Mapped[str | None] = mapped_column(String, nullable=True)
    brand: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String, default="car")
    axle_count: Mapped[int] = mapped_column(Integer, default=2)
    tire_positions: Mapped[list] = mapped_column(JSON, default=list)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_inspection: Mapped[datetime | None] = mapped_column(nullable=True)

    company: Mapped["Company | None"] = relationship(back_populates="vehicles")
    inspections: Mapped[list["Inspection"]] = relationship(back_populates="vehicle")


class Inspection(Base):
    __tablename__ = "inspections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), nullable=False, index=True)
    inspector_id: Mapped[str] = mapped_column(ForeignKey("inspectors.id"), nullable=False, index=True)
    location_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_address: Mapped[str | None] = mapped_column(String, nullable=True)
    odometer_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="completed")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    synced_at: Mapped[datetime] = mapped_column(default=utcnow)

    vehicle: Mapped["Vehicle"] = relationship(back_populates="inspections")
    inspector: Mapped["Inspector"] = relationship(back_populates="inspections")
    tires: Mapped[list["TireInspection"]] = relationship(back_populates="inspection", cascade="all, delete-orphan")


class TireInspection(Base):
    __tablename__ = "tire_inspections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    inspection_id: Mapped[str] = mapped_column(ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[str] = mapped_column(String, nullable=False)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[str | None] = mapped_column(String, nullable=True)
    dot_code: Mapped[str | None] = mapped_column(String, nullable=True)
    manufacture_date: Mapped[str | None] = mapped_column(String, nullable=True)
    tread_depth_inner: Mapped[float | None] = mapped_column(Float, nullable=True)
    tread_depth_center: Mapped[float | None] = mapped_column(Float, nullable=True)
    tread_depth_outer: Mapped[float | None] = mapped_column(Float, nullable=True)
    wear_pattern: Mapped[str | None] = mapped_column(String, nullable=True)
    condition_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_life_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pressure_psi: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommendation: Mapped[str] = mapped_column(String, default="ok")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    inspected_at: Mapped[datetime] = mapped_column(nullable=False)

    inspection: Mapped["Inspection"] = relationship(back_populates="tires")
    photos: Mapped[list["TirePhoto"]] = relationship(back_populates="tire", cascade="all, delete-orphan")


class TirePhoto(Base):
    __tablename__ = "tire_photos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tire_inspection_id: Mapped[str] = mapped_column(ForeignKey("tire_inspections.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, default="tread")
    captured_at: Mapped[datetime] = mapped_column(nullable=False)

    tire: Mapped["TireInspection"] = relationship(back_populates="photos")


class TireSpec(Base):
    """
    Catálogo de llantas por placa+posición (datos de SOLOMON).
    Sirve para el autollenado: al inspeccionar una placa, se traen
    marca/modelo/medida/última cocada conocidas de cada posición.
    """
    __tablename__ = "tire_specs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    plate: Mapped[str] = mapped_column(String, nullable=False, index=True)
    position: Mapped[str] = mapped_column(String, nullable=False)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[str | None] = mapped_column(String, nullable=True)
    last_depth_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    code: Mapped[str | None] = mapped_column(String, nullable=True)
    life: Mapped[str | None] = mapped_column(String, nullable=True)
    vehicle_type: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
