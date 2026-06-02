from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ...core.database import get_db
from ...models.models import Inspection, TireInspection, TirePhoto, Vehicle, Inspector
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/inspections", tags=["inspections"])


# ── Pydantic schemas (espejo del tipo TypeScript) ──────────────────────────

class TirePhotoIn(BaseModel):
    id: str
    uri: str
    uploadedUrl: Optional[str] = None
    type: str
    capturedAt: str


class TireInspectionIn(BaseModel):
    id: str
    inspectionId: str
    position: str
    brand: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None
    dotCode: Optional[str] = None
    manufactureDate: Optional[str] = None
    treadDepthInner: Optional[float] = None
    treadDepthCenter: Optional[float] = None
    treadDepthOuter: Optional[float] = None
    wearPattern: Optional[str] = None
    conditionScore: Optional[int] = None
    remainingLifePct: Optional[int] = None
    pressurePsi: Optional[float] = None
    recommendation: str = "ok"
    notes: Optional[str] = None
    photos: list[TirePhotoIn] = []
    inspectedAt: str


class InspectionSyncIn(BaseModel):
    id: str
    vehicleId: str
    inspectorId: str
    locationLat: Optional[float] = None
    locationLng: Optional[float] = None
    locationAddress: Optional[str] = None
    odometerKm: Optional[int] = None
    status: str
    tires: list[TireInspectionIn]
    createdAt: str
    completedAt: Optional[str] = None


class InspectionOut(BaseModel):
    id: str
    vehicleId: str
    plate: str
    vehicleLabel: str
    inspectorName: str
    status: str
    tireCount: int
    criticalCount: int
    createdAt: str
    completedAt: Optional[str]


@router.post("/sync", status_code=200)
def sync_inspection(
    body: InspectionSyncIn,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    vehicle = db.get(Vehicle, body.vehicleId)
    if not vehicle:
        raise HTTPException(404, "Vehículo no encontrado")

    # Upsert inspection
    insp = db.get(Inspection, body.id)
    if insp is None:
        insp = Inspection(id=body.id)
        db.add(insp)

    insp.vehicle_id = body.vehicleId
    insp.inspector_id = inspector.id
    insp.location_lat = body.locationLat
    insp.location_lng = body.locationLng
    insp.location_address = body.locationAddress
    insp.odometer_km = body.odometerKm
    insp.status = body.status
    insp.created_at = datetime.fromisoformat(body.createdAt)
    insp.completed_at = datetime.fromisoformat(body.completedAt) if body.completedAt else None

    # Delete existing tires and re-insert (simple upsert strategy)
    for existing_tire in insp.tires:
        db.delete(existing_tire)
    db.flush()

    for t in body.tires:
        tire = TireInspection(
            id=t.id,
            inspection_id=body.id,
            position=t.position,
            brand=t.brand,
            model=t.model,
            size=t.size,
            dot_code=t.dotCode,
            manufacture_date=t.manufactureDate,
            tread_depth_inner=t.treadDepthInner,
            tread_depth_center=t.treadDepthCenter,
            tread_depth_outer=t.treadDepthOuter,
            wear_pattern=t.wearPattern,
            condition_score=t.conditionScore,
            remaining_life_pct=t.remainingLifePct,
            pressure_psi=t.pressurePsi,
            recommendation=t.recommendation,
            notes=t.notes,
            inspected_at=datetime.fromisoformat(t.inspectedAt),
        )
        db.add(tire)

        for p in t.photos:
            url = p.uploadedUrl or p.uri
            photo = TirePhoto(
                id=p.id,
                tire_inspection_id=t.id,
                url=url,
                type=p.type,
                captured_at=datetime.fromisoformat(p.capturedAt),
            )
            db.add(photo)

    # Actualizar last_inspection del vehículo
    vehicle.last_inspection = insp.completed_at or insp.created_at
    db.commit()
    return {"ok": True}


@router.get("", response_model=list[InspectionOut])
def list_inspections(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    inspections = (
        db.query(Inspection)
        .join(Vehicle)
        .filter(Vehicle.company_id == inspector.company_id)
        .order_by(Inspection.created_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for i in inspections:
        critical = sum(1 for t in i.tires if t.recommendation in ("replace_soon", "replace_now"))
        result.append(InspectionOut(
            id=i.id,
            vehicleId=i.vehicle_id,
            plate=i.vehicle.plate,
            vehicleLabel=f"{i.vehicle.brand} {i.vehicle.model}",
            inspectorName=i.inspector.name,
            status=i.status,
            tireCount=len(i.tires),
            criticalCount=critical,
            createdAt=i.created_at.isoformat(),
            completedAt=i.completed_at.isoformat() if i.completed_at else None,
        ))
    return result
