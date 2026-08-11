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
from ...models.models import Vehicle, TireSpec, Inspector, Inspection, TireInspection, TireStock, AuditLog


def _audit(db, inspector, action, target, detail):
    """Registra un cambio en la auditoría (best-effort, no rompe si falla)."""
    try:
        db.add(AuditLog(actor=getattr(inspector, "email", None) or getattr(inspector, "name", None),
                        action=action, target=str(target)[:120], detail=str(detail)[:500],
                        company_id=inspector.company_id))
        db.commit()
    except Exception:
        db.rollback()
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/fleet", tags=["fleet"])

# ── Parámetros de negocio para "Inteligencia de costos" ──────────────────────
# Metodología TYMSAC (de sus facturas): CPK = precio ÷ km recorridos.
# Estándar de rendimiento esperado por llanta = 80.000 km.
# Precios REALES en SOLES extraídos de las facturas de TYMSAC (mediana por marca).
ESTANDAR_KM = 80000.0
NEW_TREAD_MM = 16.0    # cocada de una llanta nueva (referencia)
LIMIT_TREAD_MM = 4.0   # cocada mínima útil antes de cambio (referencia)

# Precios NUEVAS en US$ por (marca, medida) — cotizaciones reales TYMSAC 2026.
USD_PEN = 3.75  # tipo de cambio (editable)
PRICE_USD = {
    ("APLUS", "295/80R22.5"): 150.85, ("APLUS", "11R22.5"): 131.36,
    ("CARGOPOWER", "295/80R22.5"): 172.88, ("CARGOPOWER", "11R22.5"): 161.02,
    ("DURATURN", "295/80R22.5"): 237.29, ("DURATURN", "11R22.5"): 177.96,
    ("ANSU", "11R22.5"): 168, ("ANSU", "295/80R22.5"): 165,
}
# Precio NUEVA por medida (US$) si no hay marca específica.
PRICE_USD_BY_SIZE = {
    "295/80R22.5": 165, "11R22.5": 150, "12R22.5": 175,
    "425/65R22.5": 350, "275/70R22.5": 150,
}
_NEW_USD_DEFAULT = 160
# Reencauche en SOLES (cotización RELINO 2026).
RETREAD_PEN = {"11R22.5": 440, "295/80R22.5": 470, "12R22.5": 480}
_RETREAD_PEN_DEFAULT = 450
# Compat con código que use TIRE_PRICES.
TIRE_PRICES = {s: {"new": round(u * USD_PEN), "retread": RETREAD_PEN.get(s, _RETREAD_PEN_DEFAULT)}
               for s, u in PRICE_USD_BY_SIZE.items()}


def _price(size, retread=False, brand=None):
    """Precio de la llanta en SOLES. Nueva: US$ (marca+medida)×TC. Reencauche: soles."""
    s = (size or "").strip()
    if retread:
        return RETREAD_PEN.get(s, _RETREAD_PEN_DEFAULT)
    b = (brand or "").strip().upper()
    usd = PRICE_USD.get((b, s)) or PRICE_USD_BY_SIZE.get(s) or _NEW_USD_DEFAULT
    return round(usd * USD_PEN)


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

    # Nombres reales de las hojas (el archivo puede no llamarse "BD"/"CAMBIAR")
    try:
        _sheet_names = pd.ExcelFile(io.BytesIO(raw), engine=engine).sheet_names
    except Exception:
        _sheet_names = []

    def read(sheet, header):
        return pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=header, engine=engine)

    def _norm(s: str) -> str:
        """normaliza nombre de columna: sin tildes, minúsculas, sin espacios/puntos"""
        import unicodedata
        s = unicodedata.normalize("NFKD", str(s))
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return "".join(ch for ch in s.lower() if ch.isalnum())

    def _detect_header(sheet: str, keys=("codigo", "placa")) -> int:
        """Busca la fila de encabezados (la que contiene las columnas clave)."""
        try:
            probe = pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=None, nrows=15, engine=engine)
        except Exception:
            return 2
        for i in range(len(probe)):
            vals = [_norm(v) for v in probe.iloc[i].tolist()]
            hits = sum(1 for k in keys if any(k in v for v in vals))
            if hits >= len(keys):
                return i
        return 2

    def _norm_sheet(s: str) -> str:
        return _norm(s)

    def _find_sheet(preferred, keys):
        """Elige la hoja de datos: 1) por nombre preferido, 2) por contenido (keys en encabezado), 3) la más grande."""
        # 1) coincidencia por nombre (BD, base, datos…)
        for want in preferred:
            for sh in _sheet_names:
                if _norm_sheet(sh) == _norm_sheet(want):
                    return sh
        # 2) por contenido: la hoja cuyo encabezado contenga las claves
        best, best_rows = None, -1
        for sh in _sheet_names:
            try:
                probe = pd.read_excel(io.BytesIO(raw), sheet_name=sh, header=None, nrows=15, engine=engine)
            except Exception:
                continue
            found = False
            for i in range(len(probe)):
                vals = [_norm(v) for v in probe.iloc[i].tolist()]
                if all(any(k in v for v in vals) for k in keys):
                    found = True
                    break
            if found:
                try:
                    nrows = pd.read_excel(io.BytesIO(raw), sheet_name=sh, header=None, engine=engine).shape[0]
                except Exception:
                    nrows = 0
                if nrows > best_rows:
                    best, best_rows = sh, nrows
        return best

    data_sheet = _find_sheet(("BD", "base", "datos", "data"), ("codigo", "placa")) \
        or _find_sheet(("BD",), ("codigo",)) \
        or (_sheet_names[0] if _sheet_names else "BD")
    try:
        bd = read(data_sheet, _detect_header(data_sheet))
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer la hoja de datos ('{data_sheet}'). Hojas encontradas: {_sheet_names}. Detalle: {e}")

    # CAMBIAR: solo por nombre (contenido similar a BD podría confundirla)
    cam_sheet = None
    for sh in _sheet_names:
        if sh != data_sheet and _norm_sheet(sh) in ("cambiar", "correccion", "cambios"):
            cam_sheet = sh
            break
    try:
        cam = read(cam_sheet, _detect_header(cam_sheet, keys=("medida",))) if cam_sheet else None
    except Exception:
        cam = None

    def c(x):
        return str(x).strip() if pd.notna(x) else ""

    # ── Mapa de columnas reales (tolerante a tildes/variantes) ──
    colmap = {_norm(col): col for col in bd.columns}

    def C(*cands):
        """devuelve el nombre real de la primera columna que coincida"""
        for cand in cands:
            k = _norm(cand)
            if k in colmap:
                return colmap[k]
        for cand in cands:  # coincidencia parcial
            k = _norm(cand)
            for nk, real in colmap.items():
                if k and k in nk:
                    return real
        return None

    COL_COD, COL_PLACA, COL_POS = C("Codigo"), C("Placa"), C("Posicion"),
    COL_MARCA, COL_MODELO, COL_MEDIDA = C("Marca"), C("Modelo"), C("Medida")
    COL_VIDA, COL_COCADA, COL_KMTOT = C("Vida"), C("Altura Cocada", "Cocada"), C("KMTotal", "KM Total")
    COL_COND = C("Condicion")
    COL_INVO = C("Invt Orig Almacen", "Invt Orig Alma", "InvtOrig", "Invt Orig")
    COL_INVC = C("Cod.Invt Almacen", "Cod Invt Almacen", "CodInvt", "Cod.Invt Alma")

    # ── Corrección de marcas mal cargadas como "VARIOS" ──
    # El sistema SOLOMON deja muchas llantas con marca "VARIOS"; la marca real
    # se deduce del MODELO. Mapa confirmado por TYMSAC (modelo normalizado → marca).
    def _nm(s):
        return "".join(ch for ch in str(s).upper() if ch.isalnum())
    MODEL_BRAND = {
        _nm("HF668"): "SUNFULL", _nm("BYS98"): "ANSU", _nm("Y126"): "DURATURN",
        _nm("AL707"): "ANSU", _nm("BY912L"): "ANSU", _nm("GAM839"): "GITI",
        _nm("GAM837"): "GITI", _nm("FLAMA 89"): "FENIXWAY", _nm("VALOR 96"): "FENIXWAY",
    }
    # Regla especial: las delanteras compradas en 2026 vienen con código
    # CO225151 (columna "Invt Orig Almacen") → son DURATURN modelo Y237.
    # Se compara de forma tolerante (mayúsculas, sin separadores y O≡0),
    # porque el sistema mezcla la letra "O" con el cero "0".
    def _code(s):
        return "".join(ch for ch in str(s).upper() if ch.isalnum()).replace("O", "0")
    INVO_OVERRIDE = {
        _code("CO225151"): ("DURATURN", "Y237"),
        _code("C0332951"): ("DURATURN", "Y237"),
    }

    # ── Normalización de MEDIDAS ──
    # Medidas estándar de la flota: 295/80R22.5, 11R22.5, 12R22.5,
    # 425/65R22.5, 275/70R22.5. Corrige los múltiples typos del SOLOMON.
    # Las medidas de aro 20" (camiones viejos) y de camioneta (16/15/14")
    # se dejan tal cual porque son reales, no errores.
    import re as _re2
    def _norm_size(raw):
        s = str(raw).upper().strip()
        if not s:
            return raw
        s2 = _re2.sub(r"\s+\d+\s*PR\b.*$", "", s)     # quita capas "18PR" y lo que siga
        s2 = _re2.sub(r"\s+\d+[A-Z]\b.*$", "", s2)    # quita índice de carga "107S"
        t = s2.replace(" ", "").replace("..", ".")    # sin espacios, 22..5 -> 22.5
        has225 = ("2.5" in t) or ("2.8" in t) or ("2.2" in t) or t.endswith("R22")
        if not has225 and _re2.search(r"(\.00|X20|-20|R20|0020|R16|R15|R14|235/|245/|185/|LT)", t):
            # Medidas reales de camioneta / aro 20": se conservan pero limpias.
            u = t[2:] if t.startswith("LT") else t          # quita prefijo "LT"
            # aro 20": consolidar a 11.00-20 / 12.00-20 (sin ply -18/-16)
            if ("20" in u) and _re2.search(r"(\.00|X20|20-|R20|0020|-20)", u):
                if u.startswith("11"):
                    return "11.00-20"
                if u.startswith("12"):
                    return "12.00-20"
            u = _re2.sub(r"-\d+$", "", u)                    # quita ply final "-18"
            if u == "245/75R":
                return "245/75R16"                           # incompleta → aro 16
            return u                                          # ej: "245/70R16 107S" → "245/70R16"
        if t.startswith("295") or t.startswith("297"):
            return "295/80R22.5"
        if t.startswith("425") or t.startswith("426"):
            return "425/65R22.5"
        if t.startswith("275"):
            return "275/70R22.5"
        if t.startswith("12R") and "2.5" in t:
            return "12R22.5"
        if t.startswith("11R") and "2.5" in t:
            return "11R22.5"
        if t.startswith("11X"):        # p.ej. "11XZE" (banda Michelin) → 11R22.5
            return "11R22.5"
        return t

    # Recorrido de la VIDA ACTUAL = columna "Detalle Vida Original"
    # (= KMTotal - km de vidas anteriores). Para 1V equivale a KMTotal.
    COL_KMLIFE = C("Detalle Vida Original", "Detalle Vida", "Detalle Vida Actual")
    COL_ESTIMADO = C("Estimado TYM", "Estimado TYMSAC", "Estimado")

    # La columna de UBICACIÓN (estados 05. UNIDAD, 03. ALMACEN...) se detecta por CONTENIDO
    import re as _re
    COL_UBIC = None
    best = 0
    for col in bd.columns:
        try:
            sample = bd[col].dropna().astype(str).head(200)
        except Exception:
            continue
        hits = sum(1 for v in sample if _re.match(r"^\s*\d{2}\s*\.", v))
        if hits > best:
            best, COL_UBIC = hits, col
    # tipo de unidad (CARRETA/TRACTO) por contenido
    COL_TIPO = None
    for col in bd.columns:
        try:
            sample = bd[col].dropna().astype(str).head(200).str.upper()
        except Exception:
            continue
        if sum(1 for v in sample if v.strip() in ("CARRETA", "TRACTO")) > 20:
            COL_TIPO = col
            break

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

    def g(b, col):
        return c(b.get(col)) if col else ""

    # ── Mapa código → medida (para completar las que están vacías) ──
    # Para cada código de almacén (Invt Orig / Cod.Invt) se toma la medida
    # más frecuente entre las filas que SÍ la tienen; luego se usa para
    # rellenar las que vienen sin medida (p.ej. WLY-12R → 12R22.5).
    from collections import Counter as _Ctr
    _code_size_ct: dict[str, "_Ctr"] = {}
    for _i in range(len(bd)):
        _b = bd.iloc[_i]
        _md = _norm_size(g(_b, COL_MEDIDA))
        if not _md:
            continue
        for _cd in (_code(g(_b, COL_INVO)), _code(g(_b, COL_INVC))):
            if _cd:
                _code_size_ct.setdefault(_cd, _Ctr())[_md] += 1
    _code_size = {k: v.most_common(1)[0][0] for k, v in _code_size_ct.items()}

    for i in range(len(bd)):
        b = bd.iloc[i]
        m = cam.iloc[i] if aligned else None
        ubic = g(b, COL_UBIC) or "Sin ubicación"
        codigo = g(b, COL_COD)
        if aligned and m is not None:
            marca = c(m.get("Marca Cambiar")) or g(b, COL_MARCA)
            modelo = c(m.get("Modelo Cambiar")) or g(b, COL_MODELO)
            medida = c(m.get("Medida Cambiar")) or g(b, COL_MEDIDA)
        else:
            marca, modelo = g(b, COL_MARCA), g(b, COL_MODELO)
            medida_raw = g(b, COL_MEDIDA)
            medida = medida_map.get(medida_raw, medida_raw)

        # ── Corregir marca/modelo ──
        # 1) Por código de almacén (delanteras 2026 CO225151 → DURATURN Y237)
        # El código puede estar en "Invt Orig Almacen" o en "Cod.Invt Almacen"
        invo = _code(g(b, COL_INVO))
        invc = _code(g(b, COL_INVC))
        if invo in INVO_OVERRIDE or invc in INVO_OVERRIDE:
            marca, modelo = INVO_OVERRIDE.get(invo) or INVO_OVERRIDE.get(invc)
        else:
            # 2) Por modelo: reemplaza la marca real (corrige "VARIOS" y unifica)
            real = MODEL_BRAND.get(_nm(modelo))
            if real:
                marca = real
        # 3) Normalizar la medida a las 5 estándar (corrige typos del SOLOMON)
        medida = _norm_size(medida)
        # 4) Si la llanta no trae medida, completarla por su código de almacén
        if not medida:
            cd = _code(g(b, COL_INVO)) or _code(g(b, COL_INVC))
            if cd in _code_size:
                medida = _code_size[cd]

        cocada = num(b, COL_COCADA)
        vida = g(b, COL_VIDA)
        km_total = num(b, COL_KMTOT)
        vu = vida.upper()
        # Recorrido de la vida actual (columna "Detalle Vida Original")
        km_life = num(b, COL_KMLIFE)
        estimado = num(b, COL_ESTIMADO)  # meta de km de esta llanta (Estimado TYM)
        # Respaldo: si no vino, en 1V el recorrido es todo el acumulado
        if (km_life is None or km_life == 0) and vu == "1V":
            km_life = km_total
        plate = g(b, COL_PLACA).upper().replace(" ", "").replace("-", "")
        pos = g(b, COL_POS)

        ubic_counts[ubic] = ubic_counts.get(ubic, 0) + 1

        # Solo las montadas en unidad (05. UNIDAD) con placa+posición van a la flota
        if ubic.upper().startswith("05") and plate and pos:
            rec = {
                "plate": plate, "position": pos, "brand": marca, "model": modelo,
                "size": medida, "lastDepthMm": cocada, "code": codigo, "life": vida,
                "kmTotal": km_total, "kmLife": km_life, "estimado": estimado,
            }
            fleet.setdefault(plate, {"type": g(b, COL_TIPO), "tires": {}})["tires"][pos] = rec
            if codigo:
                new_by_code[codigo] = rec
        else:
            # Inventario en otras ubicaciones (almacén, reencauche, vendidas, etc.)
            stock.append({
                "code": codigo or None, "brand": marca or None, "model": modelo or None,
                "size": medida or None, "life": vida or None, "depth_mm": cocada,
                "km_total": km_total, "km_life": km_life, "estimado_km": estimado,
                "ubicacion": ubic, "plate": plate or None,
                "condicion": g(b, COL_COND) or None,
            })

    if not fleet and not stock:
        raise HTTPException(400, "El archivo no contiene filas válidas.")
    if not COL_UBIC or not COL_COD:
        raise HTTPException(400,
            "No se reconocieron las columnas del archivo (Código/Ubicación). "
            f"Columnas detectadas: {list(bd.columns)[:15]}")

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
                km_total=t.get("kmTotal"), km_life=t.get("kmLife"), estimado_km=t.get("estimado"),
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

    from .inspections import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    _audit(db, inspector, "cargar-solomon", file.filename or "archivo",
           f"{specs_created} llantas en flota · {len(stock)} en inventario")
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
    from .inspections import invalidate_dashboard_cache
    invalidate_dashboard_cache()
    _audit(db, inspector, "cambiar-estado", f"{updated} unidades",
           f"{sum(1 for i in body.items if i.active)} activas / {sum(1 for i in body.items if not i.active)} inactivas")
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
    by_brand = Counter((r.brand or "—").strip() or "—" for r in items)
    by_size = Counter((r.size or "—").strip() or "—" for r in items)
    by_life = Counter((r.life or "—").strip() or "—" for r in items)
    out = [{
        "code": r.code, "brand": r.brand, "model": r.model, "size": r.size,
        "life": r.life, "depthMm": r.depth_mm, "kmTotal": r.km_total,
        "ubicacion": r.ubicacion, "plate": r.plate, "condicion": r.condicion,
    } for r in items[:3000]]
    top = lambda cnt: [{"label": k, "count": v} for k, v in cnt.most_common(50)]
    return {
        "total": len(rows),
        "filteredCount": len(items),
        "byUbicacion": sorted([{"ubicacion": k, "count": v} for k, v in by_ubic.items()],
                              key=lambda x: x["count"], reverse=True),
        "byBrand": top(by_brand),
        "bySize": top(by_size),
        "byLife": top(by_life),
        "items": out,
        "shown": len(out),
    }


@router.get("/stats/rolling")
def rolling_stats(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Llantas RODANDO (montadas, sin repuestos) en unidades ACTIVAS, separadas
    por tractos / carretas / camionetas, cada una con su desglose para gráficos."""
    from collections import Counter

    def _p(s):
        return (s or "").upper().replace("-", "").replace(" ", "")

    def _is_spare(pos):
        p = (pos or "").upper()
        return p in ("RPT", "P13") or p.startswith("SP")

    vehicles = {_p(v.plate): v for v in db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).all()}
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()

    TYPES = {"truck": "Tractos", "trailer": "Carretas", "camioneta": "Camionetas"}
    groups = {k: {"label": v, "type": k, "tires": 0, "units": set(),
                  "byBrand": Counter(), "bySize": Counter(), "byLife": Counter()}
              for k, v in TYPES.items()}

    for s in specs:
        v = vehicles.get(_p(s.plate))
        if not v or getattr(v, "active", True) is False:
            continue
        if _is_spare(s.position):
            continue
        g = groups.get(v.type)
        if not g:
            continue
        g["tires"] += 1
        g["units"].add(_p(v.plate))
        g["byBrand"][(s.brand or "—").strip() or "—"] += 1
        g["bySize"][(s.size or "—").strip() or "—"] += 1
        life = (s.life or "—").strip().upper() or "—"
        g["byLife"]["1V" if life == "1V" else ("Reencauchada" if life.endswith("R") else life)] += 1

    def pack(g):
        top = lambda c, n=12: [{"label": k, "count": v} for k, v in c.most_common(n)]
        return {"label": g["label"], "type": g["type"], "tires": g["tires"], "units": len(g["units"]),
                "byBrand": top(g["byBrand"]), "bySize": top(g["bySize"]), "byLife": top(g["byLife"])}

    total = sum(g["tires"] for g in groups.values())
    return {"totalRolling": total, "groups": [pack(groups[k]) for k in TYPES]}


@router.get("/search-all")
def search_all(
    q: str = "",
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Búsqueda global: placas (vehículos) y llantas (por código/marca/modelo)."""
    query = (q or "").strip().upper()
    if len(query) < 2:
        return {"vehicles": [], "tires": []}
    cid = inspector.company_id

    veh = db.query(Vehicle).filter(Vehicle.company_id == cid).all()
    vhits = [{
        "plate": v.plate, "type": v.type, "brand": v.brand, "model": v.model,
        "year": v.year, "active": getattr(v, "active", True),
        "tires": len(v.tire_positions or []),
    } for v in veh if query in (v.plate or "").upper()
        or query in f"{v.brand or ''} {v.model or ''}".upper()][:20]

    def match_tire(code, brand, model, plate):
        blob = f"{code or ''} {brand or ''} {model or ''} {plate or ''}".upper()
        return query in blob

    specs = db.query(TireSpec).filter(TireSpec.company_id == cid).all()
    stock = db.query(TireStock).filter(TireStock.company_id == cid).all()
    thits = []
    for s in specs:
        if match_tire(s.code, s.brand, s.model, s.plate):
            thits.append({"code": s.code, "brand": s.brand, "model": s.model, "size": s.size,
                          "life": s.life, "depthMm": s.last_depth_mm, "kmTotal": s.km_total,
                          "plate": s.plate, "position": s.position, "where": "05. UNIDAD (montada)"})
    for r in stock:
        if match_tire(r.code, r.brand, r.model, r.plate):
            thits.append({"code": r.code, "brand": r.brand, "model": r.model, "size": r.size,
                          "life": r.life, "depthMm": r.depth_mm, "kmTotal": r.km_total,
                          "plate": r.plate, "position": None, "where": r.ubicacion})
    return {"vehicles": vhits, "tires": thits[:40], "tiresTotal": len(thits)}


@router.get("/tire/{code}")
def tire_by_code(
    code: str,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Ficha de una llanta por su código de fuego: dónde está, vida, km, cocada."""
    cid = inspector.company_id
    c = (code or "").strip().upper()
    records = []
    for s in db.query(TireSpec).filter(TireSpec.company_id == cid).all():
        if (s.code or "").strip().upper() == c:
            records.append({"where": "05. UNIDAD (montada)", "plate": s.plate, "position": s.position,
                            "brand": s.brand, "model": s.model, "size": s.size, "life": s.life,
                            "depthMm": s.last_depth_mm, "kmTotal": s.km_total, "kmLife": s.km_life})
    for r in db.query(TireStock).filter(TireStock.company_id == cid).all():
        if (r.code or "").strip().upper() == c:
            records.append({"where": r.ubicacion, "plate": r.plate, "position": None,
                            "brand": r.brand, "model": r.model, "size": r.size, "life": r.life,
                            "depthMm": r.depth_mm, "kmTotal": r.km_total, "kmLife": None,
                            "condicion": r.condicion})
    return {"code": c, "found": len(records), "records": records}


AVG_KM_MONTH = 9000.0  # km/mes promedio por unidad (editable) para estimar fechas


@router.get("/weekly-report")
def weekly_report(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Reporte semanal en HTML (para enviar por correo automáticamente)."""
    from fastapi.responses import HTMLResponse
    from datetime import datetime as _dt

    def _p(s):
        return (s or "").upper().replace("-", "").replace(" ", "")

    def _min_depth(t):
        vals = [x for x in (t.tread_depth_inner, t.tread_depth_center, t.tread_depth_outer) if x is not None]
        return min(vals) if vals else None

    vehicles = {v.plate: v for v in db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).all()}
    # llantas críticas de la última inspección de unidades activas
    from .inspections import recommend, tire_min_depth
    insps = (db.query(Inspection).join(Vehicle)
             .filter(Vehicle.company_id == inspector.company_id).all())
    latest = {}
    for i in insps:
        if getattr(i.vehicle, "active", True) is False:
            continue
        cur = latest.get(i.vehicle_id)
        if cur is None or (i.created_at or _dt.min) > (cur.created_at or _dt.min):
            latest[i.vehicle_id] = i
    crit = []
    for insp in latest.values():
        v = insp.vehicle
        for t in insp.tires:
            depth = tire_min_depth(t)
            rec = recommend(depth, v.type, t.position)
            if rec in ("replace_now", "replace_soon"):
                crit.append((v.plate, t.position, t.brand or "", depth, rec))
    crit.sort(key=lambda x: (0 if x[4] == "replace_now" else 1, x[3] if x[3] is not None else 99))
    urgent = [c for c in crit if c[4] == "replace_now"]

    rows_html = "".join(
        f"<tr><td style='padding:6px 10px'>{c[0]}</td><td style='padding:6px 10px'>{c[1]}</td>"
        f"<td style='padding:6px 10px'>{c[2]}</td><td style='padding:6px 10px'>{c[3]} mm</td>"
        f"<td style='padding:6px 10px;color:{'#e11d48' if c[4]=='replace_now' else '#ea580c'};font-weight:700'>"
        f"{'URGENTE' if c[4]=='replace_now' else 'Próximo'}</td></tr>"
        for c in crit[:60])
    today = _dt.utcnow().strftime("%d/%m/%Y")
    html = f"""<div style="font-family:Arial,sans-serif;max-width:720px;margin:auto">
      <div style="background:#0f2050;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
        <h2 style="margin:0">TYMSAC — Reporte semanal de neumáticos</h2>
        <p style="margin:4px 0 0;color:#9fd3ff">{today}</p>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 12px 12px">
        <p style="font-size:16px"><b style="color:#e11d48">{len(urgent)}</b> llantas en <b>cambio urgente</b> ·
           <b style="color:#ea580c">{len(crit)-len(urgent)}</b> próximas a cambiar.</p>
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead><tr style="background:#f3f4f6;text-align:left">
            <th style="padding:6px 10px">Placa</th><th style="padding:6px 10px">Pos.</th>
            <th style="padding:6px 10px">Marca</th><th style="padding:6px 10px">Cocada</th>
            <th style="padding:6px 10px">Estado</th></tr></thead>
          <tbody>{rows_html or '<tr><td colspan=5 style="padding:12px">Sin llantas críticas 🎉</td></tr>'}</tbody>
        </table>
        <p style="color:#6b7280;font-size:12px;margin-top:16px">Reporte automático del sistema de control de neumáticos TYMSAC.</p>
      </div>
    </div>"""
    return HTMLResponse(html)


@router.get("/audit")
def get_audit(
    limit: int = 200,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Registro de auditoría (cambios de estado, marcas, cargas)."""
    rows = (db.query(AuditLog)
            .filter(AuditLog.company_id == inspector.company_id)
            .order_by(AuditLog.created_at.desc())
            .limit(min(limit, 500)).all())
    return [{
        "at": r.created_at.isoformat() if r.created_at else None,
        "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail,
    } for r in rows]


@router.get("/predictive")
def predictive(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Alerta predictiva (km/meses restantes al límite) y rotación sugerida,
    solo unidades activas. Usa el ritmo de desgaste real de cada llanta."""
    def _p(s):
        return (s or "").upper().replace("-", "").replace(" ", "")

    def _is_spare(pos):
        p = (pos or "").upper()
        return p in ("RPT", "P13") or p.startswith("SP")

    def _limit(vtype, pos):
        p = (pos or "").upper()
        if vtype == "truck" and p in ("P01", "P02"):
            return 5.0
        return 3.0

    vehicles = {_p(v.plate): v for v in db.query(Vehicle).filter(Vehicle.company_id == inspector.company_id).all()}
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()

    pred = []
    by_unit: dict[str, list] = {}
    for s in specs:
        v = vehicles.get(_p(s.plate))
        if not v or getattr(v, "active", True) is False or _is_spare(s.position):
            continue
        depth = s.last_depth_mm
        km = float(s.km_life or 0)
        if depth is None:
            continue
        by_unit.setdefault(_p(s.plate), []).append((s.position, depth))
        worn = NEW_TREAD_MM - depth
        if km <= 0 or worn <= 0.5:
            continue
        rate = worn / km  # mm por km
        lim = _limit(v.type, s.position)
        rem_mm = depth - lim
        if rem_mm <= 0:
            rem_km = 0
        else:
            rem_km = rem_mm / rate if rate > 0 else None
        if rem_km is None:
            continue
        months = rem_km / AVG_KM_MONTH
        pred.append({
            "plate": s.plate, "position": s.position, "brand": s.brand, "model": s.model,
            "size": s.size, "code": s.code, "life": s.life, "depth": round(depth, 1),
            "limit": lim, "remainingKm": round(rem_km), "months": round(months, 1),
        })

    pred.sort(key=lambda x: x["remainingKm"])

    # Rotación sugerida: unidades con desgaste muy disparejo entre posiciones
    rotation = []
    for plate, tires in by_unit.items():
        depths = [d for _, d in tires if d is not None]
        if len(depths) < 4:
            continue
        spread = max(depths) - min(depths)
        if spread >= 4.0:  # más de 4mm de diferencia = conviene rotar
            v = vehicles.get(plate)
            deep = max(tires, key=lambda t: t[1])
            shallow = min(tires, key=lambda t: t[1])
            rotation.append({
                "plate": v.plate if v else plate,
                "spread": round(spread, 1),
                "minDepth": round(min(depths), 1), "maxDepth": round(max(depths), 1),
                "suggest": f"Mover {deep[0]} ({deep[1]}mm) → zona de {shallow[0]} ({shallow[1]}mm)",
            })
    rotation.sort(key=lambda x: x["spread"], reverse=True)

    return {
        "assumptions": {"kmMonth": AVG_KM_MONTH, "newTreadMm": NEW_TREAD_MM},
        "critical30d": sum(1 for p in pred if p["months"] <= 1),
        "critical90d": sum(1 for p in pred if p["months"] <= 3),
        "predictive": pred[:200],
        "rotation": rotation[:50],
    }


@router.get("/stats/intelligence")
def intelligence(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Inteligencia de costos y rendimiento: km/mm por marca, costo por km,
    ahorro por reencauche, proyección de compra y comparativo de marcas.
    Usa precios de referencia (TIRE_PRICES) — editables con precios reales."""
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    # Rendimiento sobre VIDAS COMPLETADAS: llantas del inventario que ya
    # terminaron una vida (tienen km de vida registrado). Es el km real logrado.
    completed = [r for r in db.query(TireStock).filter(TireStock.company_id == inspector.company_id).all()
                 if (r.km_life or 0) > 0]

    usable = max(1.0, NEW_TREAD_MM - LIMIT_TREAD_MM)

    class Agg:
        __slots__ = ("count", "retreads", "km", "worn", "depth_sum", "depth_n", "cost_new",
                     "km_new", "n_new", "km_re", "n_re", "est", "n_est", "met")
        def __init__(self):
            self.count = self.retreads = self.n_new = self.n_re = self.n_est = self.met = 0
            self.km = self.worn = self.depth_sum = self.cost_new = self.km_new = self.km_re = self.est = 0.0
            self.depth_n = 0

    brands: dict[str, Agg] = {}
    total_km = total_worn = 0.0
    retread_savings = 0.0
    # Acumuladores globales nuevas (1V) vs reencauchadas (xR)
    g_km_new = g_km_re = 0.0
    g_n_new = g_n_re = 0
    # Comparación vs "Estimado TYM" (meta por llanta)
    g_est = g_kmest = 0.0   # suma estimado / suma km (de las que tienen estimado)
    g_nest = g_met = 0

    for s in completed:
        brand = (s.brand or "—").strip() or "—"
        a = brands.setdefault(brand, Agg())
        a.count += 1
        life = (s.life or "").strip().upper()
        is_re = life.endswith("R")
        # km de la vida completada (rendimiento real logrado)
        km = float(s.km_life or 0)
        est = float(getattr(s, "estimado_km", 0) or 0)
        if est > 0:
            a.est += est; a.n_est += 1
            g_est += est; g_kmest += km; g_nest += 1
            if km >= est:
                a.met += 1; g_met += 1
        if is_re:
            a.retreads += 1
            retread_savings += (_price(s.size, brand=s.brand) - _price(s.size, retread=True, brand=s.brand))
            a.km_re += km; a.n_re += 1
            g_km_re += km; g_n_re += 1
        elif life.endswith("V"):
            a.km_new += km; a.n_new += 1
            g_km_new += km; g_n_new += 1
        depth = s.depth_mm
        if depth is not None:
            a.depth_sum += depth
            a.depth_n += 1
        worn = max(1.0, NEW_TREAD_MM - (depth if depth is not None else NEW_TREAD_MM))
        a.km += km
        a.worn += worn
        a.cost_new += _price(s.size, brand=s.brand)
        total_km += km
        total_worn += worn

    def _rend(avg):
        return round(avg / ESTANDAR_KM * 100, 1) if avg else 0

    rows = []
    for brand, a in brands.items():
        km_per_mm = a.km / a.worn if a.worn else 0
        avg_price = a.cost_new / a.count if a.count else 0
        avg_km = a.km / a.count if a.count else 0
        cost_per_km = (a.cost_new / a.km) if a.km else 0
        avg_new = a.km_new / a.n_new if a.n_new else 0
        avg_re = a.km_re / a.n_re if a.n_re else 0
        rows.append({
            "label": brand,
            "count": a.count,
            "retreads": a.retreads,
            "retreadRate": round(a.retreads / a.count * 100, 1) if a.count else 0,
            "avgDepth": round(a.depth_sum / a.depth_n, 1) if a.depth_n else None,
            "kmPerMm": round(km_per_mm),
            "avgKm": round(avg_km),
            "avgPrice": round(avg_price),
            "rendimientoPct": _rend(avg_km),
            "costPerKm": round(cost_per_km, 4),
            # desglose nuevas vs reencauchadas
            "newCount": a.n_new, "newAvgKm": round(avg_new), "newRend": _rend(avg_new),
            "reCount": a.n_re, "reAvgKm": round(avg_re), "reRend": _rend(avg_re),
            # vs Estimado TYM (meta por llanta)
            "estCount": a.n_est,
            "vsEstimadoPct": round(a.km / a.est * 100, 1) if a.est else None,
            "metRate": round(a.met / a.n_est * 100, 1) if a.n_est else None,
        })

    # ordenar por rendimiento (km promedio) descendente, solo marcas con muestra útil
    ranked = sorted([r for r in rows if r["avgKm"] > 0], key=lambda r: r["avgKm"], reverse=True)

    # Proyección de compra: llantas cerca del límite
    near = [s for s in specs if s.last_depth_mm is not None]
    need_now = sum(1 for s in near if s.last_depth_mm <= LIMIT_TREAD_MM + 1)   # ≤5mm
    need_soon = sum(1 for s in near if LIMIT_TREAD_MM + 1 < s.last_depth_mm <= LIMIT_TREAD_MM + 3)  # 5–7mm
    est_cost_now = sum(_price(s.size, brand=s.brand) for s in near if s.last_depth_mm <= LIMIT_TREAD_MM + 1)

    fleet_km_per_mm = total_km / total_worn if total_worn else 0
    avg_new_g = g_km_new / g_n_new if g_n_new else 0
    avg_re_g = g_km_re / g_n_re if g_n_re else 0
    return {
        "prices": TIRE_PRICES,
        "assumptions": {"newTreadMm": NEW_TREAD_MM, "limitTreadMm": LIMIT_TREAD_MM, "estandarKm": ESTANDAR_KM},
        "fleetKmPerMm": round(fleet_km_per_mm),
        "retreadSavings": round(retread_savings),
        # Rendimiento diferenciado nuevas (1V) vs reencauchadas (xR)
        "lifePerf": {
            "new": {"count": g_n_new, "avgKm": round(avg_new_g), "rendimientoPct": _rend(avg_new_g)},
            "retread": {"count": g_n_re, "avgKm": round(avg_re_g), "rendimientoPct": _rend(avg_re_g)},
            "retreadVsNewPct": round(avg_re_g / avg_new_g * 100, 1) if avg_new_g else 0,
        },
        # Comparación real vs "Estimado TYM" (meta por llanta)
        "vsEstimado": {
            "count": g_nest,
            "achievedPct": round(g_kmest / g_est * 100, 1) if g_est else 0,   # km logrado vs meta
            "metCount": g_met,
            "metRate": round(g_met / g_nest * 100, 1) if g_nest else 0,       # % que cumplió su meta
            "avgEstimado": round(g_est / g_nest) if g_nest else 0,
            "avgKm": round(g_kmest / g_nest) if g_nest else 0,
        },
        "purchase": {
            "needNow": need_now, "needSoon": need_soon,
            "estCostNow": round(est_cost_now),
        },
        "brands": ranked,
        "brandsByCost": sorted([r for r in ranked if r["costPerKm"] > 0], key=lambda r: r["costPerKm"])[:12],
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
    _audit(db, inspector, "editar-vehiculo", f"{updated} unidades",
           "; ".join(f"{m.plate}: {m.brand or ''} {m.model or ''} {m.year or ''}".strip() for m in body.makes[:10]))
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
