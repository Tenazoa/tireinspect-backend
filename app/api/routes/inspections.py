from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import io
from datetime import timedelta
from ...core.database import get_db
from ...models.models import Inspection, TireInspection, TirePhoto, Vehicle, Inspector, TireSpec
from ...api.deps import get_current_inspector
from ...services.pdf_report import generate_inspection_pdf, position_label

router = APIRouter(prefix="/inspections", tags=["inspections"])


def default_pressure(vehicle_type: Optional[str], position: Optional[str]) -> Optional[float]:
    """Presión recomendada (PSI): tracto 115 delanteras / 120 posteriores; carreta 129."""
    if vehicle_type == "truck":
        return 115.0 if position in ("P01", "P02") else 120.0
    if vehicle_type == "trailer":
        return 129.0
    return None


def rec_for(depth: Optional[float]) -> str:
    """Recomendación según remanente (mm) de la última inspección."""
    if depth is None:
        return "ok"
    if depth < 2:
        return "replace_now"
    if depth < 4:
        return "replace_soon"
    if depth < 6:
        return "monitor"
    return "ok"


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


@router.get("/{inspection_id}/detail")
def inspection_detail(
    inspection_id: str,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Detalle completo de una inspección con todas sus llantas."""
    insp = db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspección no encontrada")
    v = insp.vehicle
    if v.company_id != inspector.company_id:
        raise HTTPException(403, "No autorizado")

    key = (v.plate or "").upper().replace("-", "").replace(" ", "")
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    spec_lookup = {
        s.position: s for s in specs
        if (s.plate or "").upper().replace("-", "").replace(" ", "") == key
    }

    tires = []
    for t in sorted(insp.tires, key=lambda x: x.position or ""):
        sp = spec_lookup.get(t.position)
        tires.append({
            "position": position_label(t.position),
            "positionCode": t.position,
            "brand": t.brand,
            "model": t.model,
            "size": t.size,
            "depth": t.tread_depth_center,
            "recommendation": t.recommendation,
            "code": (sp.code if sp else None) or t.dot_code,
            "life": sp.life if sp else None,
            "kmTotal": sp.km_total if sp else None,
            "kmLife": sp.km_life if sp else None,
            "pressurePsi": t.pressure_psi or default_pressure(v.type, t.position),
            "photos": [p.url for p in t.photos],
            "notes": t.notes,
        })
    date = insp.completed_at or insp.created_at
    return {
        "id": insp.id,
        "plate": v.plate,
        "vehicleLabel": f"{v.brand} {v.model} {v.year or ''}".strip(),
        "inspectorName": insp.inspector.name if insp.inspector else "",
        "date": date.isoformat() if date else None,
        "odometerKm": insp.odometer_km,
        "tires": tires,
    }


# ── Fase 4: Reporte PDF ──────────────────────────────────────────────────────

@router.get("/{inspection_id}/pdf")
def inspection_pdf(
    inspection_id: str,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Genera y descarga el reporte PDF de una inspección."""
    insp = db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspección no encontrada")

    vehicle = insp.vehicle
    if vehicle.company_id != inspector.company_id:
        raise HTTPException(403, "No autorizado")

    company_name = inspector.company.name if inspector.company else "TireInspect"

    # lookup de código de fuego + vida por posición (SOLOMON)
    key = (vehicle.plate or "").upper().replace("-", "").replace(" ", "")
    specs = db.query(TireSpec).filter(TireSpec.plate.isnot(None)).all()
    spec_lookup = {
        s.position: {"code": s.code, "life": s.life}
        for s in specs
        if (s.plate or "").upper().replace("-", "").replace(" ", "") == key
    }
    pdf_bytes = generate_inspection_pdf(insp, vehicle, insp.inspector, company_name, spec_lookup)

    filename = f"inspeccion_{vehicle.plate}_{insp.created_at.strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Fase 4: Historial de vehículo ────────────────────────────────────────────

class TireHistoryPoint(BaseModel):
    date: str
    position: str
    depthMm: Optional[float]
    recommendation: str


class VehicleHistoryOut(BaseModel):
    vehicleId: str
    plate: str
    vehicleLabel: str
    totalInspections: int
    inspections: list[dict]
    depthTrend: list[TireHistoryPoint]


@router.get("/vehicle/{vehicle_id}/history", response_model=VehicleHistoryOut)
def vehicle_history(
    vehicle_id: str,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Historial completo de inspecciones de un vehículo, con tendencia de desgaste."""
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Vehículo no encontrado")
    if vehicle.company_id != inspector.company_id:
        raise HTTPException(403, "No autorizado")

    inspections = (
        db.query(Inspection)
        .filter(Inspection.vehicle_id == vehicle_id)
        .order_by(Inspection.created_at.asc())
        .all()
    )

    insp_list = []
    trend: list[TireHistoryPoint] = []
    for i in inspections:
        date_iso = (i.completed_at or i.created_at).isoformat()
        critical = sum(1 for t in i.tires if t.recommendation in ("replace_soon", "replace_now"))
        avg_depths = [t.tread_depth_center for t in i.tires if t.tread_depth_center is not None]
        avg_depth = round(sum(avg_depths) / len(avg_depths), 1) if avg_depths else None
        insp_list.append({
            "id": i.id,
            "date": date_iso,
            "inspector": i.inspector.name,
            "tireCount": len(i.tires),
            "criticalCount": critical,
            "avgDepthMm": avg_depth,
            "odometerKm": i.odometer_km,
        })
        for t in i.tires:
            if t.tread_depth_center is not None:
                trend.append(TireHistoryPoint(
                    date=date_iso, position=t.position,
                    depthMm=t.tread_depth_center, recommendation=t.recommendation,
                ))

    return VehicleHistoryOut(
        vehicleId=vehicle.id,
        plate=vehicle.plate,
        vehicleLabel=f"{vehicle.brand} {vehicle.model} {vehicle.year or ''}".strip(),
        totalInspections=len(inspections),
        inspections=insp_list,
        depthTrend=trend,
    )


# ── Generar inspecciones desde el remanente (última inspección SOLOMON) ──────

@router.post("/seed-from-specs")
def seed_from_specs(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Crea una inspección 'última inspección' por cada vehículo a partir de las
    TireSpec (remanente conocido de SOLOMON). Idempotente: regenera las
    inspecciones con id 'seed-*'. No toca inspecciones reales de la app móvil.
    """
    vehicles = db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).all()

    # specs agrupadas por placa (normalizada)
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    by_plate: dict[str, list[TireSpec]] = {}
    for s in specs:
        key = (s.plate or "").upper().replace("-", "").replace(" ", "")
        by_plate.setdefault(key, []).append(s)

    now = datetime.utcnow()
    created = 0
    tires_total = 0
    for idx, v in enumerate(vehicles):
        key = (v.plate or "").upper().replace("-", "").replace(" ", "")
        rows = by_plate.get(key, [])
        if not rows:
            continue

        seed_id = f"seed-{v.id}"
        existing = db.get(Inspection, seed_id)
        if existing:
            db.delete(existing)
            db.flush()

        # distribuir fechas en los últimos ~6 meses para la gráfica
        when = now - timedelta(days=(idx * 7) % 175)
        insp = Inspection(
            id=seed_id,
            vehicle_id=v.id,
            inspector_id=inspector.id,
            status="completed",
            created_at=when,
            completed_at=when,
            odometer_km=None,
        )
        db.add(insp)
        db.flush()

        for s in sorted(rows, key=lambda r: r.position or ""):
            depth = s.last_depth_mm
            tire = TireInspection(
                id=f"{seed_id}-{s.position}",
                inspection_id=seed_id,
                position=s.position,
                brand=s.brand,
                model=s.model,
                size=s.size,
                dot_code=s.code,
                tread_depth_center=depth,
                recommendation=rec_for(depth),
                inspected_at=when,
            )
            db.add(tire)
            tires_total += 1

        v.last_inspection = when
        created += 1

    db.commit()
    return {"vehiclesSeeded": created, "tiresSeeded": tires_total}


# ── Datos consolidados para el Dashboard (todo real) ─────────────────────────

@router.get("/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    inspections = (
        db.query(Inspection)
        .join(Vehicle)
        .filter(Vehicle.company_id == inspector.company_id)
        .all()
    )

    # última inspección por vehículo (solo unidades ACTIVAS)
    now_min = datetime.min
    latest: dict[str, Inspection] = {}
    for i in inspections:
        if getattr(i.vehicle, "active", True) is False:
            continue
        cur = latest.get(i.vehicle_id)
        if cur is None or (i.created_at or now_min) > (cur.created_at or now_min):
            latest[i.vehicle_id] = i

    total_vehicles = db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).count()

    # lookup de código de fuego + vida por placa+posición (datos SOLOMON limpios)
    spec_rows = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    spec_lookup: dict[tuple, TireSpec] = {}
    for s in spec_rows:
        key = ((s.plate or "").upper().replace("-", "").replace(" ", ""), s.position)
        spec_lookup[key] = s

    vehicles_out = []
    alerts = []
    all_depths = []
    critical_total = 0
    now = datetime.utcnow()
    month_buckets: dict[str, dict] = {}

    REC_RANK = {"replace_now": 0, "replace_soon": 1, "monitor": 2, "ok": 3}

    for insp in latest.values():
        v = insp.vehicle
        tires = []
        depths = []
        worst = "ok"
        for t in sorted(insp.tires, key=lambda x: x.position or ""):
            depth = t.tread_depth_center
            rec = t.recommendation or rec_for(depth)
            if depth is not None:
                depths.append(depth)
                all_depths.append(depth)
            if REC_RANK.get(rec, 3) < REC_RANK.get(worst, 3):
                worst = rec
            if rec in ("replace_now", "replace_soon", "monitor"):
                alerts.append({
                    "plate": v.plate,
                    "position": position_label(t.position),
                    "brand": f"{t.brand or ''} {t.model or ''}".strip(),
                    "depth": depth,
                    "rec": rec,
                })
                if rec in ("replace_now", "replace_soon"):
                    critical_total += 1
            spec = spec_lookup.get(((v.plate or "").upper().replace("-", "").replace(" ", ""), t.position))
            tires.append({
                "position": position_label(t.position),
                "brand": t.brand,
                "model": t.model,
                "size": t.size,
                "depth": depth,
                "rec": rec,
                "code": (spec.code if spec else None) or t.dot_code,
                "life": spec.life if spec else None,
                "kmTotal": spec.km_total if spec else None,
                "kmLife": spec.km_life if spec else None,
            })

        avg = round(sum(depths) / len(depths), 1) if depths else None
        date = (insp.completed_at or insp.created_at)
        vehicles_out.append({
            "plate": v.plate,
            "vehicleLabel": f"{v.brand} {v.model} {v.year or ''}".strip(),
            "inspector": insp.inspector.name if insp.inspector else "",
            "date": date.isoformat() if date else None,
            "avg": avg,
            "worst": worst,
            "tires": tires,
        })

        # bucket mensual
        if date:
            mk = date.strftime("%Y-%m")
            b = month_buckets.setdefault(mk, {"month": mk, "ok": 0, "monitor": 0, "critical": 0})
            if worst in ("replace_now", "replace_soon"):
                b["critical"] += 1
            elif worst == "monitor":
                b["monitor"] += 1
            else:
                b["ok"] += 1

    # ordenar vehículos peor primero
    vehicles_out.sort(key=lambda x: (REC_RANK.get(x["worst"], 3), x["avg"] if x["avg"] is not None else 99))
    # alertas peor primero
    alerts.sort(key=lambda a: (REC_RANK.get(a["rec"], 3), a["depth"] if a["depth"] is not None else 99))

    months = [month_buckets[k] for k in sorted(month_buckets.keys())][-6:]
    avg_depth = round(sum(all_depths) / len(all_depths), 1) if all_depths else None
    inspected = len(latest)
    this_month = sum(1 for i in latest.values() if (i.created_at or now).strftime("%Y-%m") == now.strftime("%Y-%m"))

    return {
        "stats": {
            "inspectionsThisMonth": this_month,
            "totalInspections": inspected,
            "criticalTires": critical_total,
            "vehiclesInspected": inspected,
            "totalVehicles": total_vehicles,
            "fleetPct": round(inspected / total_vehicles * 100) if total_vehicles else 0,
            "avgDepth": avg_depth,
        },
        "months": months,
        "alerts": alerts[:12],
        "vehicles": vehicles_out,
    }
