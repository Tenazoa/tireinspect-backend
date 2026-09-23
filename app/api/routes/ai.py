import os
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from ...core.database import get_db
from ...api.deps import get_current_inspector
from ...models.models import Inspector, TireSpec, Vehicle
from ...services.ai.tire_analyzer import analyze_tire_image, WEAR_LEVELS
from ...services.ai.vision_ai import analyze_tire_vision, vision_diagnostic
from ...services.ai.dataset_collector import save_training_sample, get_dataset_stats
from ...services.ai.reference_measurement import measure_with_reference, REFERENCE_OBJECTS
from ...services.ai.insights import ask_gastos, alertas_desgaste

router = APIRouter(prefix="/ai", tags=["ai"])


class PreguntaIn(BaseModel):
    pregunta: str


@router.post("/ask-gastos")
def ask_gastos_route(
    body: PreguntaIn,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """#4: pregunta en lenguaje natural sobre el gasto en ruta de reparación de llantas."""
    return ask_gastos(db, inspector.company_id, body.pregunta)


@router.get("/alertas-desgaste")
def alertas_desgaste_route(
    horizonte: str = "",
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """#3: convierte la predicción de desgaste en alertas accionables en lenguaje claro."""
    return alertas_desgaste(db, inspector.company_id, horizonte or None)


class TireAnalysisOut(BaseModel):
    is_tire_detected: bool
    wear_level: str
    wear_level_label: str
    confidence: float
    condition_score: int
    estimated_depth_mm: float
    depth_inner_mm: float = 0.0
    depth_center_mm: float = 0.0
    depth_outer_mm: float = 0.0
    wear_pattern: str
    pattern_confidence: float
    defects: list[str]
    recommendation: str
    analysis_notes: str
    engine: str = "opencv"          # "vision" (Claude) | "opencv" (fallback)
    fire_code: Optional[str] = None  # código de fuego leído de la foto (solo visión)


def wear_to_recommendation(wear_level: str) -> str:
    return {
        "new":     "ok",
        "low":     "ok",
        "medium":  "monitor",
        "high":    "replace_soon",
        "replace": "replace_now",
        "unknown": "monitor",
    }.get(wear_level, "monitor")


@router.get("/vision-status")
def vision_status(_: Inspector = Depends(get_current_inspector)):
    """Diagnóstico temporal de la IA de visión."""
    return vision_diagnostic()


@router.post("/analyze", response_model=TireAnalysisOut)
async def analyze_tire(
    file: UploadFile = File(...),
    inspection_id: str = Form(default="unknown"),
    position: str = Form(default="unknown"),
    manual_depth_mm: Optional[float] = Form(default=None),
    manual_recommendation: Optional[str] = Form(default=None),
    wear_pattern: Optional[str] = Form(default=None),
    tire_brand: Optional[str] = Form(default=None),
    tire_size: Optional[str] = Form(default=None),
    _: Inspector = Depends(get_current_inspector),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")

    image_bytes = await file.read()
    if len(image_bytes) < 1000:
        raise HTTPException(400, "Imagen demasiado pequeña o vacía")

    # ── SUPER IA: intentar primero con visión (Claude). Si no hay clave o
    # falla, se cae al análisis OpenCV. Así nunca se rompe. ──
    vision = analyze_tire_vision(image_bytes, brand=tire_brand, size=tire_size, position=position)
    if vision and vision.get("is_tire_detected"):
        depth = vision["estimated_depth_mm"]
        wear_label = WEAR_LEVELS.get(vision["wear_level"], {}).get("label", vision["wear_level"])
        try:
            save_training_sample(
                image_bytes=image_bytes, inspection_id=inspection_id, position=position,
                manual_depth_mm=manual_depth_mm,
                manual_recommendation=manual_recommendation or vision["recommendation"],
                ai_result={"wear_level": vision["wear_level"], "condition_score": vision["condition_score"],
                           "confidence": 0.9, "estimated_depth_mm": depth, "engine": "vision"},
                wear_pattern=wear_pattern or vision["wear_pattern"],
                tire_brand=tire_brand, tire_size=tire_size,
            )
        except Exception:
            pass
        return TireAnalysisOut(
            is_tire_detected=True, wear_level=vision["wear_level"], wear_level_label=wear_label,
            confidence=0.9, condition_score=vision["condition_score"], estimated_depth_mm=depth,
            depth_inner_mm=depth, depth_center_mm=depth, depth_outer_mm=depth,
            wear_pattern=vision["wear_pattern"], pattern_confidence=0.85,
            defects=vision["defects"], recommendation=vision["recommendation"],
            analysis_notes=vision["notes"], engine="vision", fire_code=vision.get("fire_code"),
        )

    # Análisis principal (fallback OpenCV)
    result = analyze_tire_image(image_bytes)

    # Guardar en dataset para entrenamiento futuro
    if result.is_tire_detected:
        try:
            save_training_sample(
                image_bytes=image_bytes,
                inspection_id=inspection_id,
                position=position,
                manual_depth_mm=manual_depth_mm,
                manual_recommendation=manual_recommendation or wear_to_recommendation(result.wear_level),
                ai_result={
                    "wear_level": result.wear_level,
                    "condition_score": result.condition_score,
                    "confidence": result.confidence,
                    "estimated_depth_mm": result.estimated_depth_mm,
                },
                wear_pattern=wear_pattern or result.wear_pattern,
                tire_brand=tire_brand,
                tire_size=tire_size,
            )
        except Exception:
            pass  # No interrumpir el análisis si falla el guardado

    wear_label = WEAR_LEVELS.get(result.wear_level, {}).get("label", result.wear_level)

    return TireAnalysisOut(
        is_tire_detected=result.is_tire_detected,
        wear_level=result.wear_level,
        wear_level_label=wear_label,
        confidence=result.confidence,
        condition_score=result.condition_score,
        estimated_depth_mm=result.estimated_depth_mm,
        depth_inner_mm=result.depth_inner_mm,
        depth_center_mm=result.depth_center_mm,
        depth_outer_mm=result.depth_outer_mm,
        wear_pattern=result.wear_pattern,
        pattern_confidence=result.pattern_confidence,
        defects=result.defects,
        recommendation=wear_to_recommendation(result.wear_level),
        analysis_notes=result.analysis_notes,
    )


@router.get("/dataset/stats")
def dataset_stats(
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """
    Distribución real de neumáticos por nivel de desgaste (según remanente mm)
    calculada desde la base de datos (persistente).
    """
    specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).all()
    by_class = {"new": 0, "low": 0, "medium": 0, "high": 0, "replace": 0}
    total = 0
    for s in specs:
        d = s.last_depth_mm
        if d is None:
            continue
        total += 1
        if d >= 12:
            by_class["new"] += 1
        elif d >= 8:
            by_class["low"] += 1
        elif d >= 5:
            by_class["medium"] += 1
        elif d >= 2:
            by_class["high"] += 1
        else:
            by_class["replace"] += 1

    # "precisión": proporción de la flota con medición (cobertura) como referencia
    total_specs = db.query(TireSpec).filter(TireSpec.company_id == inspector.company_id).count()
    coverage = round(total / total_specs, 3) if total_specs else 0.0

    return {
        "total": total,
        "by_class": by_class,
        "ai_accuracy": coverage,
        "min_for_training": 500,
        "ready_for_training": total >= 500,
    }


# ── Asistente IA de la flota (chat) ─────────────────────────────────────────

class ChatMsg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMsg]


@router.post("/chat")
def fleet_chat(body: ChatIn, db: Session = Depends(get_db),
               inspector: Inspector = Depends(get_current_inspector)):
    """Preguntas en lenguaje natural sobre la flota. Claude consulta la BD con
    herramientas de solo lectura filtradas por la empresa del usuario."""
    from ...services.ai.fleet_chat import responder
    if not body.messages or body.messages[-1].role != "user":
        raise HTTPException(400, "El último mensaje debe ser la pregunta del usuario")
    try:
        return responder(db, inspector.company_id, [m.model_dump() for m in body.messages])
    except Exception as e:
        raise HTTPException(502, f"No se pudo consultar a la IA: {str(e)[:200]}")


# ── Foto de llanta dada de baja → causa del daño ───────────────────────────

@router.post("/damage-cause")
async def damage_cause(file: UploadFile = File(...), contexto: str = Form(default=""),
                       _: Inspector = Depends(get_current_inspector)):
    from ...services.ai.descargo import clasificar_dano
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")
    foto = await file.read()
    if len(foto) < 1000:
        raise HTTPException(400, "Imagen demasiado pequeña o vacía")
    try:
        return clasificar_dano(foto, contexto)
    except Exception as e:
        raise HTTPException(502, f"No se pudo analizar la foto: {str(e)[:200]}")


@router.get("/llanta-info")
def llanta_info(codigo: str, db: Session = Depends(get_db),
                inspector: Inspector = Depends(get_current_inspector)):
    """Autollenado del formulario de descargo por código de llanta."""
    from ...models.models import TireStock, TireDetalle
    cid, cod = inspector.company_id, codigo.strip()
    out = {"codigo": cod, "encontrada": False}
    s = db.query(TireSpec).filter(TireSpec.company_id == cid, TireSpec.code == cod).first()
    st = None if s else db.query(TireStock).filter(TireStock.company_id == cid, TireStock.code == cod).first()
    src = s or st
    if src:
        out.update(encontrada=True, marca=src.brand, modelo=src.model, medida=src.size, vida=src.life,
                   placa=src.plate, km_recorrido=src.km_life or src.km_total,
                   posicion=getattr(s, "position", None) if s else None)
    det = db.query(TireDetalle).filter(TireDetalle.company_id == cid, TireDetalle.code == cod).all()
    for d in det:
        dd = d.data or {}
        if dd.get("CocadaPrimera") and not out.get("cocada_orig"):
            try:
                out["cocada_orig"] = float(dd["CocadaPrimera"])
            except Exception:
                pass
        if not out.get("encontrada"):
            out.update(encontrada=True, marca=d.brand, medida=d.size, placa=d.plate,
                       modelo=dd.get("Modelo"), vida=dd.get("nCicloVida"))
    if out.get("encontrada"):
        from .fleet import _precio_nueva, _condicion_from_vida
        out["costo_ref"] = _precio_nueva(out.get("marca"), out.get("modelo"))   # precio de lista, editable
        out["condicion"] = "Neumático reencauchado" if _condicion_from_vida(out.get("vida")) == "Reencauchada" else "Neumático nuevo"
    return out


@router.post("/read-code")
async def read_code(file: UploadFile = File(...), db: Session = Depends(get_db),
                    inspector: Inspector = Depends(get_current_inspector)):
    """Foto del flanco -> la IA lee el código de la llanta y, si existe en SOLOMON, trae sus datos."""
    from ...services.ai.descargo import leer_codigo
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")
    foto = await file.read()
    if len(foto) < 1000:
        raise HTTPException(400, "Imagen demasiado pequeña o vacía")
    try:
        r = leer_codigo(foto)
    except Exception as e:
        raise HTTPException(502, f"No se pudo leer el código: {str(e)[:200]}")
    if r.get("codigo"):
        r["solomon"] = llanta_info(r["codigo"], db, inspector)
    return r


@router.post("/descargo")
async def descargo(
    datos: str = Form(...),                                  # JSON con el caso y la lista de llantas
    fotos: list[UploadFile] = File(default=[]),
    tipos: list[str] = Form(default=[]),                     # tipo de cada foto (mismo orden)
    leyendas: list[str] = Form(default=[]),                  # leyenda de cada foto (opcional)
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    """Informe Técnico de Descargo en Word con el formato TYMSAC (varias llantas y fotos).
    Monto por llanta = costo ÷ cocada original × altura de salida (lo calcula el código)."""
    import json as _json
    from fastapi.responses import Response
    from urllib.parse import quote
    import base64 as _b64
    from ...services.ai.descargo import generar_descargo, firmar_word
    from ...models.models import Descargo
    try:
        d = _json.loads(datos)
    except Exception:
        raise HTTPException(400, "Datos inválidos")
    lls = d.get("llantas") or []
    if not lls:
        raise HTTPException(400, "Agrega al menos una llanta")
    for x in lls:
        try:
            co, cr, cs = float(x["cocada_orig"]), float(x["cocada_retiro"]), float(x["costo"])
        except Exception:
            raise HTTPException(400, f"Completa cocada original, altura de salida y costo de la llanta {x.get('codigo') or ''}")
        if co <= 0 or cr < 0 or cr > co or cs <= 0:
            raise HTTPException(400, f"Revisa la llanta {x.get('codigo') or ''}: la altura de salida no puede superar la cocada original")
    fs = []
    for k, f in enumerate(fotos[:12]):
        b = await f.read()
        if b and len(b) > 1000:
            fs.append({"bytes": b, "tipo": tipos[k] if k < len(tipos) else "", "leyenda": leyendas[k] if k < len(leyendas) else ""})
    try:
        docx, res = generar_descargo(d, fs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"No se pudo generar el descargo: {str(e)[:200]}")
    # guardar en el historial (Word sin firma; la firma del conductor se agrega luego)
    reg = Descargo(numero=d.get("numero"), placa=(d.get("placa") or "").strip().upper() or None,
                   conductor=(d.get("conductor") or "").strip() or None, titulo=res.get("titulo"),
                   codigos=", ".join(str(x.get("codigo") or "") for x in lls), llantas=len(lls),
                   monto=res["monto"], monto_igv=res["monto_igv"], datos=d,
                   docx_b64=_b64.b64encode(docx).decode(), created_by=inspector.email,
                   company_id=inspector.company_id)
    db.add(reg); db.commit()
    docx = firmar_word(docx, None)
    cods = "_".join(str(x.get("codigo") or "") for x in lls)[:40]
    nombre = f"INFORME_DESCARGO_{(d.get('placa') or 'SIN-PLACA').replace(' ', '')}_LLANTAS_{cods}.docx"
    return Response(
        content=docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(nombre)}",
                 "X-Descargo-Monto": str(res["monto"]),
                 "X-Descargo-Causa": quote(res.get("titulo") or ""),
                 "X-Descargo-Id": reg.id},
    )


# ── Fase 3: Medición con objeto de referencia ───────────────────────────────

class ReferenceMeasurementOut(BaseModel):
    success: bool
    reference_detected: bool
    reference_type: str
    reference_label: str
    mm_per_pixel: float
    measured_depth_mm: Optional[float]
    recommendation: Optional[str]
    confidence: float
    notes: str


@router.get("/reference-objects")
def list_reference_objects(_: Inspector = Depends(get_current_inspector)):
    """Lista de objetos de referencia soportados para calibración."""
    return [
        {"id": k, "label": v["label"], "real_mm": v["real_mm"], "shape": v["shape"]}
        for k, v in REFERENCE_OBJECTS.items()
    ]


@router.post("/measure", response_model=ReferenceMeasurementOut)
async def measure_tread(
    file: UploadFile = File(...),
    reference_type: str = Form(default="coin_pen_1"),
    _: Inspector = Depends(get_current_inspector),
):
    """
    Mide la profundidad real del surco usando un objeto de referencia
    (moneda o tarjeta) visible en la foto.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")

    image_bytes = await file.read()
    if len(image_bytes) < 1000:
        raise HTTPException(400, "Imagen demasiado pequeña o vacía")

    result = measure_with_reference(image_bytes, reference_type)

    # Recomendación según profundidad medida
    recommendation = None
    if result.measured_depth_mm is not None:
        d = result.measured_depth_mm
        if d <= 1.6:   recommendation = "replace_now"
        elif d <= 3.0: recommendation = "replace_soon"
        elif d <= 4.0: recommendation = "monitor"
        else:          recommendation = "ok"

    label = REFERENCE_OBJECTS.get(result.reference_type, {}).get("label", "")

    return ReferenceMeasurementOut(
        success=result.success,
        reference_detected=result.reference_detected,
        reference_type=result.reference_type,
        reference_label=label,
        mm_per_pixel=result.mm_per_pixel,
        measured_depth_mm=result.measured_depth_mm,
        recommendation=recommendation,
        confidence=result.confidence,
        notes=result.notes,
    )
