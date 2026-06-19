import os
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from ...core.database import get_db
from ...api.deps import get_current_inspector
from ...models.models import Inspector, TireSpec, Vehicle
from ...services.ai.tire_analyzer import analyze_tire_image, WEAR_LEVELS
from ...services.ai.dataset_collector import save_training_sample, get_dataset_stats
from ...services.ai.reference_measurement import measure_with_reference, REFERENCE_OBJECTS

router = APIRouter(prefix="/ai", tags=["ai"])


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


def wear_to_recommendation(wear_level: str) -> str:
    return {
        "new":     "ok",
        "low":     "ok",
        "medium":  "monitor",
        "high":    "replace_soon",
        "replace": "replace_now",
        "unknown": "monitor",
    }.get(wear_level, "monitor")


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

    # Análisis principal
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
