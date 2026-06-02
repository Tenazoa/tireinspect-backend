import os
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import Optional
from ...api.deps import get_current_inspector
from ...models.models import Inspector
from ...services.ai.tire_analyzer import analyze_tire_image, WEAR_LEVELS
from ...services.ai.dataset_collector import save_training_sample, get_dataset_stats

router = APIRouter(prefix="/ai", tags=["ai"])


class TireAnalysisOut(BaseModel):
    is_tire_detected: bool
    wear_level: str
    wear_level_label: str
    confidence: float
    condition_score: int
    estimated_depth_mm: float
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
        wear_pattern=result.wear_pattern,
        pattern_confidence=result.pattern_confidence,
        defects=result.defects,
        recommendation=wear_to_recommendation(result.wear_level),
        analysis_notes=result.analysis_notes,
    )


@router.get("/dataset/stats")
def dataset_stats(_: Inspector = Depends(get_current_inspector)):
    """Estadísticas del dataset acumulado para entrenamiento."""
    return get_dataset_stats()
