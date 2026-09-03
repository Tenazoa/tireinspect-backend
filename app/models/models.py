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
    active: Mapped[bool] = mapped_column(Boolean, default=True)

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


class PhotoBlob(Base):
    """Imagen de inspección guardada en la BD (persiste en Supabase)."""
    __tablename__ = "photo_blobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    content_type: Mapped[str] = mapped_column(String, default="image/jpeg")
    data: Mapped[str] = mapped_column(Text, nullable=False)  # base64
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TireDetalle(Base):
    """Reporte 'Detalle de Llantas' (LL.018.00) de SOLOMON: historial rico por
    llanta (cocada primera/actual, fechas, chofer, observaciones, etc.).
    Se guardan campos clave indexados + todo lo demás en `data` (JSON)."""
    __tablename__ = "tire_detalle"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    code: Mapped[str | None] = mapped_column(String, nullable=True, index=True)   # nroLlanta
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[str | None] = mapped_column(String, nullable=True)
    plate: Mapped[str | None] = mapped_column(String, nullable=True)
    estado: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # TipoEstado
    tipo_unidad: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)                 # todas las columnas
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


class TireStock(Base):
    """
    Llantas que NO están montadas en unidades (ubicación distinta de '05. UNIDAD'):
    almacén, reencauche, ciclo final, vendidas, etc. (datos de SOLOMON).
    """
    __tablename__ = "tire_stock"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    code: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[str | None] = mapped_column(String, nullable=True)
    life: Mapped[str | None] = mapped_column(String, nullable=True)
    depth_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_life: Mapped[float | None] = mapped_column(Float, nullable=True)  # km de la vida (Detalle Vida)
    estimado_km: Mapped[float | None] = mapped_column(Float, nullable=True)  # meta "Estimado TYM"
    ubicacion: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    plate: Mapped[str | None] = mapped_column(String, nullable=True)
    condicion: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


class AuditLog(Base):
    """Registro de auditoría: quién cambió qué y cuándo."""
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str | None] = mapped_column(String, nullable=True)      # email/nombre
    action: Mapped[str | None] = mapped_column(String, nullable=True)     # p.ej. set-status
    target: Mapped[str | None] = mapped_column(String, nullable=True)     # placa / código
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)       # descripción del cambio
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


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
    km_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_life: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimado_km: Mapped[float | None] = mapped_column(Float, nullable=True)  # meta "Estimado TYM"
    vehicle_type: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


class VehicleInfo(Base):
    """Ficha de la unidad (reporte SOLOMON 'Listado de Unidades' / trplacas):
    marca, tipo, estado operativo, KM actual y vencimientos de documentos."""
    __tablename__ = "vehicle_info"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    plate: Mapped[str] = mapped_column(String, nullable=False, index=True)
    marca: Mapped[str | None] = mapped_column(String, nullable=True)
    modelo: Mapped[str | None] = mapped_column(String, nullable=True)
    tipo: Mapped[str | None] = mapped_column(String, nullable=True)          # TRACTO / CARRETA
    ejes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ruedas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)      # Estado = Operativa
    estado: Mapped[str | None] = mapped_column(String, nullable=True)        # texto original
    condicion: Mapped[str | None] = mapped_column(String, nullable=True)
    tipo_carga: Mapped[str | None] = mapped_column(String, nullable=True)
    tipo_servicio: Mapped[str | None] = mapped_column(String, nullable=True)
    km_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    fv_soat: Mapped[str | None] = mapped_column(String, nullable=True)       # dd/mm/yyyy
    fv_citv: Mapped[str | None] = mapped_column(String, nullable=True)
    fv_segveh: Mapped[str | None] = mapped_column(String, nullable=True)
    fv_chv: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


class VehicleKm(Base):
    """Kilometraje recorrido por unidad y mes (reporte SOLOMON 'Resumen de
    Kilometrajes' / trrepresumenkm). Un registro por placa+año+mes."""
    __tablename__ = "vehicle_km"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    plate: Mapped[str] = mapped_column(String, nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False)              # 1..12
    km: Mapped[float | None] = mapped_column(Float, nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)


class CarretaLink(Base):
    """Historial de cambios de carreta por tracto (reporte SOLOMON
    'Placas Nro Cambio de Carretas' / vtrplacasnrocarretas_detalle)."""
    __tablename__ = "carreta_link"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_uuid)
    tracto: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    carreta: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    fecha: Mapped[str | None] = mapped_column(String, nullable=True)         # dd/mm/yyyy
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
