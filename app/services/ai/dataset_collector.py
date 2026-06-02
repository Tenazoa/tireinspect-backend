"""
Colector de dataset — Data Flywheel.

Cada inspección con medición manual del inspector genera datos etiquetados
automáticamente. Con ~3,000-5,000 imágenes se puede reentrenar un modelo
EfficientNet con alta precisión.

Estructura de archivos:
  dataset/
    images/
      {inspection_id}_{position}_{uuid}.jpg
    labels/
      {inspection_id}_{position}_{uuid}.json  ← etiqueta + metadata
    manifest.csv   ← índice general para entrenamiento
"""

import os
import json
import csv
import uuid
from datetime import datetime
from pathlib import Path

DATASET_DIR = Path(__file__).parent.parent.parent.parent / "dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
MANIFEST   = DATASET_DIR / "manifest.csv"

IMAGES_DIR.mkdir(parents=True, exist_ok=True)
LABELS_DIR.mkdir(parents=True, exist_ok=True)


def save_training_sample(
    image_bytes: bytes,
    inspection_id: str,
    position: str,
    manual_depth_mm: float | None,
    manual_recommendation: str,
    ai_result: dict,
    wear_pattern: str | None = None,
    tire_brand: str | None = None,
    tire_size: str | None = None,
) -> str:
    """
    Guarda una imagen + su etiqueta para el dataset de entrenamiento.
    Retorna el ID del sample guardado.
    """
    sample_id = str(uuid.uuid4())[:8]
    filename = f"{inspection_id[:8]}_{position}_{sample_id}"

    # Guardar imagen
    img_path = IMAGES_DIR / f"{filename}.jpg"
    with open(img_path, "wb") as f:
        f.write(image_bytes)

    # Calcular clase de desgaste desde medición manual (gold standard)
    if manual_depth_mm is not None:
        ground_truth = _depth_to_class(manual_depth_mm)
        label_source = "manual_measurement"
    else:
        ground_truth = manual_recommendation
        label_source = "inspector_judgment"

    # Guardar etiqueta JSON
    label = {
        "sample_id": sample_id,
        "filename": f"{filename}.jpg",
        "created_at": datetime.utcnow().isoformat(),
        "ground_truth": {
            "wear_level": ground_truth,
            "depth_mm": manual_depth_mm,
            "recommendation": manual_recommendation,
            "wear_pattern": wear_pattern,
            "label_source": label_source,
        },
        "ai_prediction": ai_result,
        "metadata": {
            "inspection_id": inspection_id,
            "position": position,
            "tire_brand": tire_brand,
            "tire_size": tire_size,
        },
    }
    label_path = LABELS_DIR / f"{filename}.json"
    with open(label_path, "w", encoding="utf-8") as f:
        json.dump(label, f, ensure_ascii=False, indent=2)

    # Agregar al manifest CSV
    _append_manifest(filename, ground_truth, manual_depth_mm, ai_result.get("wear_level"), ai_result.get("confidence"))

    return sample_id


def get_dataset_stats() -> dict:
    """Estadísticas del dataset acumulado."""
    manifest_rows = _read_manifest()
    total = len(manifest_rows)
    if total == 0:
        return {"total": 0, "by_class": {}, "ai_accuracy": 0}

    by_class: dict[str, int] = {}
    correct = 0
    for row in manifest_rows:
        cls = row.get("ground_truth", "unknown")
        by_class[cls] = by_class.get(cls, 0) + 1
        if row.get("ground_truth") == row.get("ai_prediction"):
            correct += 1

    return {
        "total": total,
        "by_class": by_class,
        "ai_accuracy": round(correct / total, 3) if total > 0 else 0,
        "min_for_training": 500,
        "ready_for_training": total >= 500,
    }


def _depth_to_class(depth_mm: float) -> str:
    if depth_mm >= 7.0:  return "new"
    if depth_mm >= 5.0:  return "low"
    if depth_mm >= 3.0:  return "medium"
    if depth_mm >= 1.7:  return "high"
    return "replace"


def _append_manifest(filename: str, ground_truth: str, depth: float | None, ai_pred: str | None, ai_conf: float | None):
    write_header = not MANIFEST.exists()
    with open(MANIFEST, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["filename", "ground_truth", "depth_mm", "ai_prediction", "ai_confidence", "created_at"])
        writer.writerow([filename, ground_truth, depth or "", ai_pred or "", ai_conf or "", datetime.utcnow().isoformat()])


def _read_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with open(MANIFEST, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))
