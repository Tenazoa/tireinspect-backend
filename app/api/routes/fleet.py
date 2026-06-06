"""
Carga de flota desde SOLOMON y autollenado de inspección.

- POST /fleet/import   : importa el catálogo de llantas (placa+posición → marca/modelo/medida)
                         y crea/actualiza los vehículos. Solo admin/supervisor.
- GET  /fleet/{plate}  : devuelve las llantas conocidas de una placa (autollenado)
"""
import uuid
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
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


@router.post("/upload-solomon")
async def upload_solomon(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Carga el Excel del sistema SOLOMON (hojas BD + CAMBIAR), limpia los datos
    (medida/marca/modelo corregidos por CAMBIAR, alineado por fila) y actualiza
    el catálogo de llantas. Devuelve la lista de llantas que faltan respecto al
    estado anterior (códigos de fuego que ya no aparecen) con toda su descripción.
    """
    import pandas as pd

    raw = await file.read()
    name = (file.filename or "").lower()
    engine = "xlrd" if name.endswith(".xls") else "openpyxl"

    def read(sheet, header):
        return pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=header, engine=engine)

    try:
        bd = read("BD", 2)
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer la hoja 'BD': {e}")
    try:
        cam = read("CAMBIAR", 1)
    except Exception:
        cam = None

    def c(x):
        return str(x).strip() if pd.notna(x) else ""

    company_id = inspector.company_id
    n = len(bd) if cam is None else min(len(bd), len(cam))

    fleet: dict[str, dict] = {}
    new_by_code: dict[str, dict] = {}

    for i in range(n):
        b = bd.iloc[i]
        m = cam.iloc[i] if cam is not None else None
        plate = c(b.get("Placa")).upper().replace(" ", "").replace("-", "")
        pos = c(b.get("Posicion"))
        if not plate or not pos:
            continue
        if m is not None:
            marca = c(m.get("Marca Cambiar")) or c(b.get("Marca"))
            modelo = c(m.get("Modelo Cambiar")) or c(b.get("Modelo"))
            medida = c(m.get("Medida Cambiar")) or c(b.get("Medida"))
        else:
            marca, modelo, medida = c(b.get("Marca")), c(b.get("Modelo")), c(b.get("Medida"))
        try:
            cocada = float(b.get("Altura Cocada")) if pd.notna(b.get("Altura Cocada")) else None
        except Exception:
            cocada = None
        vida = c(b.get("Vida"))
        codigo = c(b.get("Codigo"))
        tipo = c(b.get("Ubicación")) or c(b.get("T.Unidad"))
        rec = {
            "plate": plate, "position": pos, "brand": marca, "model": modelo,
            "size": medida, "lastDepthMm": cocada, "code": codigo, "life": vida,
        }
        fleet.setdefault(plate, {"type": tipo, "tires": {}})["tires"][pos] = rec
        if codigo:
            new_by_code[codigo] = rec

    if not fleet:
        raise HTTPException(400, "El archivo no contiene filas válidas (Placa/Posición).")

    # ── Diff contra el estado anterior (por código de fuego) ──
    existing = db.query(TireSpec).filter(TireSpec.company_id == company_id).all()
    old_by_code = {s.code: s for s in existing if s.code}

    missing = [
        {
            "code": s.code, "plate": s.plate, "position": s.position,
            "brand": s.brand, "model": s.model, "size": s.size,
            "life": s.life, "lastDepthMm": s.last_depth_mm,
        }
        for code, s in old_by_code.items() if code not in new_by_code
    ]
    added = [
        {
            "code": r["code"], "plate": r["plate"], "position": r["position"],
            "brand": r["brand"], "model": r["model"], "size": r["size"],
            "life": r["life"], "lastDepthMm": r["lastDepthMm"],
        }
        for code, r in new_by_code.items() if code not in old_by_code
    ]
    missing.sort(key=lambda x: (x["plate"] or "", x["position"] or ""))
    added.sort(key=lambda x: (x["plate"] or "", x["position"] or ""))

    # ── Upsert: reemplaza specs por placa, conserva marca/modelo del vehículo ──
    vehicles_created = 0
    specs_created = 0
    for plate, v in fleet.items():
        positions = list(v["tires"].keys())
        vtype = _infer_type(v["type"], len(positions))
        vehicle = db.query(Vehicle).filter(Vehicle.plate == plate).first()
        if not vehicle:
            vehicle = Vehicle(
                id=str(uuid.uuid4()), plate=plate, brand="—", model="—",
                type=vtype, axle_count=3, tire_positions=positions, company_id=company_id,
            )
            db.add(vehicle)
            vehicles_created += 1
        else:
            vehicle.tire_positions = positions
            vehicle.type = vtype
        db.query(TireSpec).filter(TireSpec.plate == plate).delete()
        for pos, t in v["tires"].items():
            db.add(TireSpec(
                id=str(uuid.uuid4()), plate=plate, position=pos,
                brand=t["brand"], model=t["model"], size=t["size"],
                last_depth_mm=t["lastDepthMm"], code=t["code"], life=t["life"],
                vehicle_type=vtype, company_id=company_id,
            ))
            specs_created += 1

    db.commit()

    return {
        "ok": True,
        "fileName": file.filename,
        "vehicles": len(fleet),
        "vehiclesCreated": vehicles_created,
        "tireSpecs": specs_created,
        "missingCount": len(missing),
        "addedCount": len(added),
        "missing": missing[:1000],
        "added": added[:1000],
    }


@router.get("/stats/analytics")
def fleet_analytics(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Análisis de la flota de llantas: cantidades por marca, modelo y medida;
    nuevas (1V) vs reencauchadas (xR); e índices de reencauche/reencauchabilidad.
    """
    import re
    from collections import Counter

    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    total = len(specs)

    by_brand = Counter()
    by_model = Counter()
    by_size = Counter()
    by_life = Counter()
    new_count = 0
    retread_count = 0
    retread_levels = 0  # suma de niveles de reencauche (1R=1, 2R=2, ...)

    for s in specs:
        brand = (s.brand or "—").strip() or "—"
        model = f"{brand} {(s.model or '').strip()}".strip()
        size = (s.size or "—").strip() or "—"
        by_brand[brand] += 1
        by_model[model] += 1
        by_size[size] += 1

        life = (s.life or "").strip().upper()
        m = re.match(r"(\d+)\s*([VR])", life)
        if m:
            num, letter = int(m.group(1)), m.group(2)
            if letter == "V":
                new_count += 1
                by_life[f"{num}V"] += 1
            else:  # R
                retread_count += 1
                retread_levels += num
                by_life[f"{num}R"] += 1
        else:
            by_life["Sin dato"] += 1

    def top(counter, n=None):
        items = [{"label": k, "count": v} for k, v in counter.most_common(n)]
        return items

    retread_rate = round(retread_count / total * 100, 1) if total else 0
    new_rate = round(new_count / total * 100, 1) if total else 0
    # promedio de reencauches por carcasa reencauchada
    retreadability = round(retread_levels / retread_count, 2) if retread_count else 0
    # relación reencauchadas / nuevas
    ratio_r_n = round(retread_count / new_count, 2) if new_count else 0

    # ordenar by_life de forma natural (1V, 1R, 2R, 3R, ...)
    def life_key(item):
        s = item["label"]
        mm = re.match(r"(\d+)([VR])", s)
        if not mm:
            return (99, 9)
        return (int(mm.group(1)), 0 if mm.group(2) == "V" else 1)
    life_list = sorted(top(by_life), key=life_key)

    return {
        "total": total,
        "newCount": new_count,
        "newRate": new_rate,
        "retreadCount": retread_count,
        "retreadRate": retread_rate,          # Índice de reencauche (%)
        "retreadabilityIndex": retreadability, # Índice de reencauchabilidad (reencauches/carcasa reencauchada)
        "ratioRetreadNew": ratio_r_n,         # reencauchadas por cada nueva
        "byLife": life_list,
        "byBrand": top(by_brand),
        "byModel": top(by_model),
        "bySize": top(by_size),
    }


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
