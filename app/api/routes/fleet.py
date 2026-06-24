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
from ...models.models import Vehicle, TireSpec, Inspector, Inspection, TireInspection, TireStock
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

    # Si CAMBIAR está alineado por fila (mismo nº filas) usamos sus columnas corregidas;
    # si no (archivo histórico grande), usamos BD + corrección global de medida.
    aligned = cam is not None and len(cam) == len(bd)
    medida_map: dict[str, str] = {}
    if cam is not None and not aligned:
        amb = set()
        for _, r in cam.iterrows():
            md, mdc = c(r.get("Medida")), c(r.get("Medida Cambiar"))
            if md and mdc and md != mdc:
                if md in medida_map and medida_map[md] != mdc:
                    amb.add(md)
                else:
                    medida_map[md] = mdc
        for k in amb:
            medida_map.pop(k, None)

    UNIDAD = "05. UNIDAD"
    fleet: dict[str, dict] = {}
    new_by_code: dict[str, dict] = {}
    stock: list[dict] = []
    ubic_counts: dict[str, int] = {}

    def num(b, col):
        try:
            return float(b.get(col)) if pd.notna(b.get(col)) else None
        except Exception:
            return None

    for i in range(len(bd)):
        b = bd.iloc[i]
        m = cam.iloc[i] if aligned else None
        ubic = c(b.get("Ubicacion")) or c(b.get("Ubicación 2")) or "Sin ubicación"
        codigo = c(b.get("Codigo"))
        if aligned and m is not None:
            marca = c(m.get("Marca Cambiar")) or c(b.get("Marca"))
            modelo = c(m.get("Modelo Cambiar")) or c(b.get("Modelo"))
            medida = c(m.get("Medida Cambiar")) or c(b.get("Medida"))
        else:
            marca, modelo = c(b.get("Marca")), c(b.get("Modelo"))
            medida_raw = c(b.get("Medida"))
            medida = medida_map.get(medida_raw, medida_raw)
        cocada = num(b, "Altura Cocada")
        vida = c(b.get("Vida"))
        km_total = num(b, "KMTotal")
        vu = vida.upper()
        km_col = {"1V": "KM1", "1R": "KM2", "2R": "KM3", "3R": "KM4"}.get(vu)
        km_life = num(b, km_col) if km_col else None
        plate = c(b.get("Placa")).upper().replace(" ", "").replace("-", "")
        pos = c(b.get("Posicion"))

        ubic_counts[ubic] = ubic_counts.get(ubic, 0) + 1

        # Solo las montadas en unidad (05. UNIDAD) con placa+posición van a la flota
        if ubic == UNIDAD and plate and pos:
            rec = {
                "plate": plate, "position": pos, "brand": marca, "model": modelo,
                "size": medida, "lastDepthMm": cocada, "code": codigo, "life": vida,
                "kmTotal": km_total, "kmLife": km_life,
            }
            fleet.setdefault(plate, {"type": c(b.get("Ubicación")), "tires": {}})["tires"][pos] = rec
            if codigo:
                new_by_code[codigo] = rec
        else:
            # Inventario en otras ubicaciones (almacén, reencauche, vendidas, etc.)
            stock.append({
                "code": codigo or None, "brand": marca or None, "model": modelo or None,
                "size": medida or None, "life": vida or None, "depth_mm": cocada,
                "km_total": km_total, "ubicacion": ubic, "plate": plate or None,
                "condicion": c(b.get("Condicion")) or None,
            })

    if not fleet and not stock:
        raise HTTPException(400, "El archivo no contiene filas válidas.")

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
            # conservar el tipo ya asignado (tracto/carreta/camioneta); no sobre-escribir
        db.query(TireSpec).filter(TireSpec.plate == plate).delete()
        for pos, t in v["tires"].items():
            db.add(TireSpec(
                id=str(uuid.uuid4()), plate=plate, position=pos,
                brand=t["brand"], model=t["model"], size=t["size"],
                last_depth_mm=t["lastDepthMm"], code=t["code"], life=t["life"],
                km_total=t.get("kmTotal"), km_life=t.get("kmLife"),
                vehicle_type=vtype, company_id=company_id,
            ))
            specs_created += 1

    # ── Inventario (otras ubicaciones): reemplazar todo ──
    db.query(TireStock).filter(TireStock.company_id == company_id).delete()
    for srec in stock:
        db.add(TireStock(id=str(uuid.uuid4()), company_id=company_id, **srec))
    db.commit()

    ubic_list = sorted(
        [{"ubicacion": k, "count": v} for k, v in ubic_counts.items()],
        key=lambda x: x["count"], reverse=True,
    )

    return {
        "ok": True,
        "fileName": file.filename,
        "vehicles": len(fleet),
        "vehiclesCreated": vehicles_created,
        "tireSpecs": specs_created,
        "stockCount": len(stock),
        "byUbicacion": ubic_list,
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


@router.get("/debug/companies")
def debug_companies(db: Session = Depends(get_db), _: Inspector = Depends(get_current_inspector)):
    from ...models.models import Company
    out = []
    for c in db.query(Company).all():
        vs = db.query(Vehicle).filter(Vehicle.company_id == c.id).all()
        inactive = sum(1 for v in vs if getattr(v, "active", True) is False)
        users = db.query(Inspector).filter(Inspector.company_id == c.id).count()
        out.append({"company": c.name, "id": c.id, "vehicles": len(vs), "inactive": inactive, "users": users})
    # vehiculos sin empresa
    orphan = db.query(Vehicle).filter(Vehicle.company_id.is_(None)).count()
    return {"companies": out, "orphanVehicles": orphan, "totalVehicles": db.query(Vehicle).count()}


class VehicleStatusIn(BaseModel):
    plate: str
    active: bool


class StatusImportIn(BaseModel):
    items: list[VehicleStatusIn]


@router.post("/set-status")
def set_status(
    body: StatusImportIn,
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    """Marca vehículos como activos/inactivos por placa (SITUACIONAL FLOTA)."""
    updated = 0
    not_found = []
    for it in body.items:
        plate = it.plate.strip().upper().replace("-", "").replace(" ", "")
        v = db.query(Vehicle).filter(Vehicle.plate == plate).first()
        if v:
            v.active = it.active
            updated += 1
        else:
            not_found.append(plate)
    db.commit()
    return {"ok": True, "updated": updated, "notFoundCount": len(not_found), "notFound": not_found[:30]}


@router.get("/stats/performance")
def fleet_performance(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Rendimiento de neumáticos (vista gerencial): mejores marcas/modelos y mejores
    unidades según remanente promedio (mm) y durabilidad (reencauches alcanzados).
    """
    import re
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()

    def agg():
        return {"sum": 0.0, "n": 0, "crit": 0, "retreads": 0, "lifes": 0}

    by_brand, by_model, by_plate = {}, {}, {}
    for s in specs:
        d = s.last_depth_mm
        brand = (s.brand or "—").strip() or "—"
        model = f"{brand} {(s.model or '').strip()}".strip()
        plate = s.plate
        life = (s.life or "").strip().upper()
        m = re.match(r"(\d+)([VR])", life)
        retread_lvl = int(m.group(1)) if (m and m.group(2) == "R") else 0
        for key, store in ((brand, by_brand), (model, by_model), (plate, by_plate)):
            a = store.setdefault(key, agg())
            if d is not None:
                a["sum"] += d; a["n"] += 1
                if d < 4: a["crit"] += 1
            a["lifes"] += 1
            a["retreads"] += retread_lvl

    def rows(store, min_n):
        out = []
        for k, a in store.items():
            if a["n"] < min_n:
                continue
            out.append({
                "label": k, "count": a["n"],
                "avgDepth": round(a["sum"] / a["n"], 1) if a["n"] else 0,
                "criticalPct": round(a["crit"] / a["n"] * 100) if a["n"] else 0,
                "avgRetread": round(a["retreads"] / a["lifes"], 2) if a["lifes"] else 0,
            })
        return out

    brands = sorted(rows(by_brand, 20), key=lambda x: x["avgDepth"], reverse=True)
    models = sorted(rows(by_model, 10), key=lambda x: x["avgDepth"], reverse=True)
    plates = sorted(rows(by_plate, 4), key=lambda x: x["avgDepth"], reverse=True)
    durable = sorted(rows(by_brand, 20), key=lambda x: x["avgRetread"], reverse=True)

    return {
        "bestBrands": brands[:10],
        "worstBrands": brands[::-1][:5],
        "bestModels": models[:10],
        "bestVehicles": plates[:10],
        "attentionVehicles": plates[::-1][:10],
        "mostDurableBrands": durable[:6],
    }


@router.get("/stock")
def fleet_stock(
    ubicacion: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Inventario de llantas en ubicaciones distintas de la unidad (almacén, reencauche, etc.)."""
    from collections import Counter
    rows = db.query(TireStock).filter(TireStock.company_id == inspector.company_id).all()
    by_ubic = Counter(r.ubicacion or "Sin ubicación" for r in rows)
    items = rows
    if ubicacion and ubicacion != "all":
        items = [r for r in items if (r.ubicacion or "") == ubicacion]
    if search:
        q = search.upper()
        items = [r for r in items if (r.code or "").upper().find(q) >= 0
                 or f"{r.brand or ''} {r.model or ''}".upper().find(q) >= 0]
    out = [{
        "code": r.code, "brand": r.brand, "model": r.model, "size": r.size,
        "life": r.life, "depthMm": r.depth_mm, "kmTotal": r.km_total,
        "ubicacion": r.ubicacion, "plate": r.plate, "condicion": r.condicion,
    } for r in items[:3000]]
    return {
        "total": len(rows),
        "byUbicacion": sorted([{"ubicacion": k, "count": v} for k, v in by_ubic.items()],
                              key=lambda x: x["count"], reverse=True),
        "items": out,
        "shown": len(out),
    }


@router.get("/stats/fleet")
def fleet_vehicle_stats(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Estadísticas de unidades: por tipo, tractos por año/modelo, carretas por tipo, activos/inactivos."""
    from collections import Counter
    vs = db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).all()

    def active(v):
        return getattr(v, "active", True) is not False

    total = len(vs)
    active_count = sum(1 for v in vs if active(v))

    by_type = {}
    for v in vs:
        t = v.type or "otro"
        d = by_type.setdefault(t, {"type": t, "total": 0, "active": 0, "inactive": 0})
        d["total"] += 1
        d["active" if active(v) else "inactive"] += 1

    tractos = [v for v in vs if v.type == "truck"]
    carretas = [v for v in vs if v.type == "trailer"]
    camionetas = [v for v in vs if v.type == "camioneta"]

    def top(counter):
        return [{"label": str(k), "count": v} for k, v in counter.most_common()]

    tractos_year = Counter(str(v.year) if v.year else "Sin año" for v in tractos)
    tractos_model = Counter(f"{v.brand} {v.model}".strip() for v in tractos)
    carretas_type = Counter((v.brand or "—").strip() for v in carretas)
    carretas_year = Counter(str(v.year) if v.year else "Sin año" for v in carretas)

    def sort_year(items):
        return sorted(items, key=lambda x: (x["label"] == "Sin año", x["label"]))

    return {
        "total": total,
        "active": active_count,
        "inactive": total - active_count,
        "byType": [
            {"label": {"truck": "Tractos", "trailer": "Carretas", "camioneta": "Camionetas", "van": "Furgones", "car": "Autos"}.get(k, k),
             "type": k, **{kk: vv for kk, vv in d.items() if kk != "type"}}
            for k, d in by_type.items()
        ],
        "tractosTotal": len(tractos),
        "tractosActive": sum(1 for v in tractos if active(v)),
        "carretasTotal": len(carretas),
        "carretasActive": sum(1 for v in carretas if active(v)),
        "camionetasTotal": len(camionetas),
        "camionetasActive": sum(1 for v in camionetas if active(v)),
        "tractosByYear": sort_year(top(tractos_year)),
        "tractosByModel": top(tractos_model),
        "carretasByType": top(carretas_type),
        "carretasByYear": sort_year(top(carretas_year)),
    }


@router.get("/tires-to-change")
def tires_to_change(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Lista de llantas que requieren cambio (urgente o próximo), separadas por
    tractos y carretas, según la última inspección de cada unidad.
    """
    from .inspections import position_label, tire_min_depth

    inspections = (
        db.query(Inspection).join(Vehicle)
        .filter(Vehicle.company_id == inspector.company_id).all()
    )
    latest = {}
    nowmin = __import__("datetime").datetime.min
    for i in inspections:
        cur = latest.get(i.vehicle_id)
        if cur is None or (i.created_at or nowmin) > (cur.created_at or nowmin):
            latest[i.vehicle_id] = i

    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    spec_lookup = {}
    for s in specs:
        key = ((s.plate or "").upper().replace("-", "").replace(" ", ""), s.position)
        spec_lookup[key] = s

    tractos, carretas, camionetas = [], [], []
    for insp in latest.values():
        v = insp.vehicle
        # Solo unidades ACTIVAS (las inactivas/en reparación no entran al cambio)
        if getattr(v, "active", True) is False:
            continue
        plate_key = (v.plate or "").upper().replace("-", "").replace(" ", "")
        for t in insp.tires:
            if t.recommendation not in ("replace_now", "replace_soon"):
                continue
            sp = spec_lookup.get((plate_key, t.position))
            rec = {
                "plate": v.plate,
                "vehicle": f"{v.brand} {v.model} {v.year or ''}".strip(),
                "position": position_label(t.position),
                "brand": t.brand,
                "model": t.model,
                "size": t.size,
                "code": (sp.code if sp else None) or t.dot_code,
                "life": sp.life if sp else None,
                "kmLife": sp.km_life if sp else None,
                "kmTotal": sp.km_total if sp else None,
                "depth": tire_min_depth(t),
                "recommendation": t.recommendation,
            }
            if v.type == "truck":
                tractos.append(rec)
            elif v.type == "camioneta":
                camionetas.append(rec)
            else:
                carretas.append(rec)

    keyf = lambda x: (x["depth"] if x["depth"] is not None else 99, x["plate"] or "")
    tractos.sort(key=keyf)
    carretas.sort(key=keyf)
    camionetas.sort(key=keyf)
    return {
        "tractos": tractos,
        "carretas": carretas,
        "camionetas": camionetas,
        "tractosCount": len(tractos),
        "carretasCount": len(carretas),
        "camionetasCount": len(camionetas),
    }


class VehicleMakeIn(BaseModel):
    plate: str
    brand: str
    model: Optional[str] = "Tracto"
    year: Optional[int] = None
    type: Optional[str] = None  # 'truck' (tracto) | 'trailer' (carreta)


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
        plate = m.plate.strip().upper().replace("-", "").replace(" ", "")
        v = db.query(Vehicle).filter(Vehicle.plate == plate).first()
        if v:
            v.brand = m.brand
            v.model = m.model or "Tracto"
            if m.year:
                v.year = m.year
            if m.type in ("truck", "trailer", "camioneta", "van", "car"):
                v.type = m.type
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
    kmTotal: Optional[float] = None
    kmLife: Optional[float] = None
    pressurePsi: Optional[float] = None


def _pressure(vehicle_type: Optional[str], position: str) -> Optional[float]:
    if vehicle_type == "truck":
        return 115.0 if position in ("P01", "P02") else 120.0
    if vehicle_type == "trailer":
        return 120.0
    return None


@router.get("/{plate}", response_model=list[TireSpecOut])
def get_fleet_tires(
    plate: str,
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    """Autollenado: llantas conocidas de una placa (marca/modelo/medida/última cocada/presión)."""
    p = plate.strip().upper().replace("-", "").replace(" ", "")
    vehicle = db.query(Vehicle).filter(Vehicle.plate == p).first()
    vtype = vehicle.type if vehicle else None
    specs = (
        db.query(TireSpec)
        .filter(TireSpec.plate == p)
        .order_by(TireSpec.position)
        .all()
    )
    return [
        TireSpecOut(
            position=s.position, brand=s.brand, model=s.model, size=s.size,
            lastDepthMm=s.last_depth_mm, code=s.code, life=s.life,
            kmTotal=s.km_total, kmLife=s.km_life,
            pressurePsi=_pressure(vtype, s.position),
        )
        for s in specs
    ]
