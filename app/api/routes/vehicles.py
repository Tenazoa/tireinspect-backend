from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ...core.database import get_db
from ...models.models import Vehicle, Inspector
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


class VehicleOut(BaseModel):
    id: str
    plate: str
    vin: Optional[str]
    brand: str
    model: str
    year: Optional[int]
    type: str
    axleCount: int
    tirePositions: list[str]
    ownerCompany: Optional[str]
    createdAt: str
    lastInspection: Optional[str]

    class Config:
        from_attributes = True


class VehicleCreate(BaseModel):
    plate: str
    vin: Optional[str] = None
    brand: str
    model: str
    year: Optional[int] = None
    type: str = "car"
    axleCount: int = 2
    tirePositions: list[str] = ["FL", "FR", "RL", "RR"]


def vehicle_to_out(v: Vehicle) -> VehicleOut:
    return VehicleOut(
        id=v.id,
        plate=v.plate,
        vin=v.vin,
        brand=v.brand,
        model=v.model,
        year=v.year,
        type=v.type,
        axleCount=v.axle_count,
        tirePositions=v.tire_positions or [],
        ownerCompany=v.company.name if v.company else None,
        createdAt=v.created_at.isoformat(),
        lastInspection=v.last_inspection.isoformat() if v.last_inspection else None,
    )


@router.get("/search", response_model=list[VehicleOut])
def search_vehicles(
    plate: str = Query(..., min_length=2),
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    vehicles = db.query(Vehicle).filter(
        Vehicle.plate.ilike(f"%{plate}%")
    ).order_by(Vehicle.last_inspection.desc().nullslast()).limit(10).all()
    return [vehicle_to_out(v) for v in vehicles]


@router.get("/my-fleet", response_model=list[VehicleOut])
def my_fleet(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    vehicles = db.query(Vehicle).filter(
        Vehicle.company_id == inspector.company_id
    ).order_by(Vehicle.last_inspection.desc().nullslast()).all()
    return [vehicle_to_out(v) for v in vehicles]


@router.post("", response_model=VehicleOut, status_code=201)
def create_vehicle(
    body: VehicleCreate,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    import uuid
    vehicle = Vehicle(
        id=str(uuid.uuid4()),
        plate=body.plate.upper(),
        vin=body.vin,
        brand=body.brand,
        model=body.model,
        year=body.year,
        type=body.type,
        axle_count=body.axleCount,
        tire_positions=body.tirePositions,
        company_id=inspector.company_id,
    )
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle_to_out(vehicle)
