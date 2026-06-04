"""
Carga de flota desde SOLOMON y autollenado de inspección.

- POST /fleet/import   : importa el catálogo de llantas (placa+posición → marca/modelo/medida)
                         y crea/actualiza los vehículos. Solo admin/supervisor.
- GET  /fleet/{plate}  : devuelve las llantas conocidas de una placa (autollenado)
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from ...core.database import get_db
from ...models.models import Vehicle, TireSpec, Inspector
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/fleet", tags=["fleet"])


class TireSpecIn(BaseModel):
    position: str
    brand: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    lastDepthMm: Optional[float] = None
    code: Optional[str] = None
    life: Optional[str] = None


class VehicleImportIn(BaseModel):
    plate: str
    type: Optional[str] = None
    tires: list[TireSpecIn]


class FleetImportIn(BaseModel):
    vehicles: list[VehicleImportIn]


def _infer_type(solomon_type: str | None, n_tires: int) -> str:
    t = (solomon_type or "").upper()
    if "CARRETA" in t or "SEMI" in t or "REMOLQ" in t:
        return "trailer"
    if "TRACTO" in t or "CAMION" in t or "VOLQ" in t:
        return "truck"
    if n_tires >= 10:
        return "truck"
    return "truck"


@router.post("/import")
def import_fleet(
    body: FleetImportIn,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Importa el catálogo de flota desde SOLOMON. Reemplaza specs existentes por placa."""
    company_id = inspector.company_id
    vehicles_created = 0
    specs_created = 0

    for v in body.vehicles:
        plate = v.plate.strip().upper()
        if not plate:
            continue
        positions = [t.position for t in v.tires]
        vtype = _infer_type(v.type, len(positions))

        # Upsert vehicle
        vehicle = db.query(Vehicle).filter(Vehicle.plate == plate).first()
        if not vehicle:
            vehicle = Vehicle(
                id=str(uuid.uuid4()), plate=plate, brand="—", model="—",
                type=vtype, axle_count=3, tire_positions=positions,
                company_id=company_id,
            )
            db.add(vehicle)
            vehicles_created += 1
        else:
            vehicle.tire_positions = positions
            vehicle.type = vtype

        # Reemplazar specs de esta placa
        db.query(TireSpec).filter(TireSpec.plate == plate).delete()
        for t in v.tires:
            db.add(TireSpec(
                id=str(uuid.uuid4()), plate=plate, position=t.position,
                brand=t.brand, model=t.model, size=t.size,
                last_depth_mm=t.lastDepthMm, code=t.code, life=t.life,
                vehicle_type=vtype, company_id=company_id,
            ))
            specs_created += 1

    db.commit()
    return {"ok": True, "vehiclesCreated": vehicles_created, "tireSpecs": specs_created}


class VehicleMakeIn(BaseModel):
    plate: str
    brand: str
    model: Optional[str] = "Tracto"


class MakesImportIn(BaseModel):
    makes: list[VehicleMakeIn]


@router.post("/update-makes")
def update_makes(
    body: MakesImportIn,
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    """Actualiza marca/modelo del vehículo por placa (datos de SITUACIONAL FLOTA)."""
    updated = 0
    not_found = []
    for m in body.makes:
        plate = m.plate.strip().upper()
        v = db.query(Vehicle).filter(Vehicle.plate == plate).first()
        if v:
            v.brand = m.brand
            v.model = m.model or "Tracto"
            updated += 1
        else:
            not_found.append(plate)
    db.commit()
    return {"ok": True, "updated": updated, "notFound": not_found[:20], "notFoundCount": len(not_found)}


class TireSpecOut(BaseModel):
    position: str
    brand: Optional[str]
    model: Optional[str]
    size: Optional[str]
    lastDepthMm: Optional[float]
    code: Optional[str]
    life: Optional[str]


@router.get("/{plate}", response_model=list[TireSpecOut])
def get_fleet_tires(
    plate: str,
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    """Autollenado: llantas conocidas de una placa (marca/modelo/medida/última cocada)."""
    specs = (
        db.query(TireSpec)
        .filter(TireSpec.plate == plate.strip().upper())
        .order_by(TireSpec.position)
        .all()
    )
    return [
        TireSpecOut(
            position=s.position, brand=s.brand, model=s.model, size=s.size,
            lastDepthMm=s.last_depth_mm, code=s.code, life=s.life,
        )
        for s in specs
    ]
