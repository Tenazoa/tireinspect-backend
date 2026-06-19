"""
Motor de análisis de neumáticos — Fase 2.

Pipeline de análisis por imagen usando OpenCV + heurísticas calibradas.
Diseñado para ser reemplazado progresivamente por un modelo TFLite entrenado
con el dataset que se va acumulando de cada inspección.

Métricas analizadas:
  1. Textura de la banda de rodamiento (contraste local → profundidad estimada)
  2. Distribución de iluminación (oscuridad uniforme → desgaste avanzado)
  3. Detección de bordes (densidad de surcos visibles)
  4. Análisis de color (goma desgastada cambia de negro profundo a gris claro)
  5. Uniformidad espacial (detecta desgaste irregular por zonas)
"""

import cv2
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class TireAnalysisResult:
    # Clasificación principal
    wear_level: str          # new | low | medium | high | replace
    confidence: float        # 0.0 - 1.0
    condition_score: int     # 0-100 (100 = llanta nueva)
    estimated_depth_mm: float  # estimación de profundidad en mm (centro)
    depth_inner_mm: float = 0.0   # lado interior
    depth_center_mm: float = 0.0  # centro
    depth_outer_mm: float = 0.0   # lado exterior

    # Patrón de desgaste
    wear_pattern: str        # uniform | center | edge_both | edge_inner | edge_outer | cupping | diagonal
    pattern_confidence: float

    # Defectos detectados
    defects: list[str]       # crack, bulge, irregular, bald_spot, etc.

    # Metadatos
    is_tire_detected: bool   # ¿La imagen contiene una llanta?
    analysis_notes: str


WEAR_LEVELS = {
    "new":     {"score_range": (85, 100), "depth_range": (7.0, 8.5), "label": "Nueva / Sin desgaste"},
    "low":     {"score_range": (65, 84),  "depth_range": (5.0, 6.9), "label": "Desgaste leve"},
    "medium":  {"score_range": (40, 64),  "depth_range": (3.0, 4.9), "label": "Desgaste moderado"},
    "high":    {"score_range": (20, 39),  "depth_range": (1.7, 2.9), "label": "Desgaste avanzado"},
    "replace": {"score_range": (0,  19),  "depth_range": (0.0, 1.6), "label": "Reemplazo urgente"},
}


def analyze_tire_image(image_bytes: bytes) -> TireAnalysisResult:
    """
    Análisis principal de imagen de neumático.
    Entrada: bytes de imagen JPEG/PNG
    Salida: TireAnalysisResult con todos los parámetros calculados
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return _error_result("No se pudo decodificar la imagen")

        # Resize a resolución estándar para análisis consistente
        img = cv2.resize(img, (640, 480))

        # 1. Verificar que la imagen contiene una llanta
        is_tire, tire_confidence = _detect_tire_presence(img)
        if not is_tire:
            return _error_result("No se detectó una llanta en la imagen")

        # 2. Extraer ROI (región de banda de rodamiento)
        roi = _extract_tread_roi(img)

        # 3. Analizar textura (indicador principal de profundidad de surco)
        texture_score = _analyze_texture(roi)

        # 4. Analizar oscuridad/color (llanta desgastada = más gris)
        color_score = _analyze_color(roi)

        # 5. Densidad de bordes (surcos visibles)
        edge_score = _analyze_edges(roi)

        # 6. Patrón de desgaste por zonas
        wear_pattern, pattern_conf = _analyze_wear_pattern(roi)

        # 7. Detectar defectos
        defects = _detect_defects(img, roi)

        # Score final ponderado
        condition_score = int(
            texture_score * 0.45 +
            color_score   * 0.30 +
            edge_score    * 0.25
        )
        condition_score = max(0, min(100, condition_score))

        # Nivel de desgaste desde score
        wear_level, confidence = _score_to_wear_level(condition_score)

        # Profundidad estimada (interpolación lineal desde score)
        estimated_depth = _score_to_depth(condition_score)

        # Profundidad por zona (interior / centro / exterior) según textura de cada tercio
        d_in, d_ce, d_ou = _zone_depths(roi, estimated_depth)

        notes = _build_notes(wear_level, wear_pattern, defects, texture_score, color_score)

        return TireAnalysisResult(
            wear_level=wear_level,
            confidence=round(confidence, 2),
            condition_score=condition_score,
            estimated_depth_mm=round(estimated_depth, 1),
            depth_inner_mm=d_in,
            depth_center_mm=d_ce,
            depth_outer_mm=d_ou,
            wear_pattern=wear_pattern,
            pattern_confidence=round(pattern_conf, 2),
            defects=defects,
            is_tire_detected=True,
            analysis_notes=notes,
        )

    except Exception as e:
        return _error_result(f"Error de análisis: {str(e)}")


# ── Funciones auxiliares ─────────────────────────────────────────────────────

def _zone_depths(roi: np.ndarray, base_depth: float) -> tuple[float, float, float]:
    """
    Estima la profundidad en 3 zonas (interior, centro, exterior) según la
    textura de cada tercio horizontal de la banda. Más textura = más surco.
    """
    try:
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        thirds = [gray[:, :w // 3], gray[:, w // 3:2 * w // 3], gray[:, 2 * w // 3:]]
        texs = []
        for z in thirds:
            lap = cv2.Laplacian(z, cv2.CV_64F).var()
            texs.append(float(lap))
        avg = sum(texs) / 3 if sum(texs) > 0 else 1.0
        out = []
        for t in texs:
            factor = t / avg if avg > 0 else 1.0
            # limitar variación a ±35% del valor base
            factor = max(0.65, min(1.35, factor))
            out.append(round(max(0.5, min(9.0, base_depth * factor)), 1))
        return out[0], out[1], out[2]
    except Exception:
        return base_depth, base_depth, base_depth


def _detect_tire_presence(img: np.ndarray) -> tuple[bool, float]:
    """Detecta si la imagen contiene una llanta usando análisis de círculos y oscuridad."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Las llantas tienden a ser oscuras (goma negra)
    dark_ratio = np.sum(gray < 80) / gray.size

    # Buscar bordes circulares (perfil de llanta)
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    circles = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.2,
                                minDist=100, param1=50, param2=30,
                                minRadius=50, maxRadius=250)

    has_circles = circles is not None
    confidence = min(dark_ratio * 1.5, 1.0) * (1.2 if has_circles else 0.8)
    confidence = min(confidence, 1.0)

    # Umbral de detección: imagen oscura + preferiblemente con forma circular
    is_tire = dark_ratio > 0.15 or has_circles
    return bool(is_tire), float(confidence)


def _extract_tread_roi(img: np.ndarray) -> np.ndarray:
    """Extrae la región central (banda de rodamiento) donde está el desgaste."""
    h, w = img.shape[:2]
    # Centro horizontal, 60% del ancho, 50% del alto
    x1, x2 = int(w * 0.2), int(w * 0.8)
    y1, y2 = int(h * 0.25), int(h * 0.75)
    return img[y1:y2, x1:x2]


def _analyze_texture(roi: np.ndarray) -> float:
    """
    Analiza la textura de la banda de rodamiento.
    Llanta nueva → surcos profundos → alta varianza de textura → score alto
    Llanta desgastada → superficie lisa → baja varianza → score bajo
    """
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    variance = laplacian.var()

    # Calibración: variance < 50 = muy liso (desgastado), variance > 400 = muy texturizado (nuevo)
    score = np.interp(variance, [20, 50, 150, 300, 500], [5, 20, 50, 80, 100])
    return float(score)


def _analyze_color(roi: np.ndarray) -> float:
    """
    Analiza el color de la goma.
    Goma nueva: negro profundo (bajo valor en HSV)
    Goma desgastada: gris claro (mayor brillo, menor saturación)
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    brightness = hsv[:, :, 2].mean()  # Canal Value
    saturation = hsv[:, :, 1].mean()  # Canal Saturation

    # Llanta nueva es muy oscura (brightness ~30-60)
    # Llanta desgastada es más gris (brightness ~80-140)
    brightness_score = np.interp(brightness, [30, 60, 90, 120, 160], [95, 80, 55, 30, 5])
    sat_bonus = np.interp(saturation, [0, 20, 60], [0, 5, 15])  # más saturado = mejor

    return float(min(brightness_score + sat_bonus, 100))


def _analyze_edges(roi: np.ndarray) -> float:
    """
    Analiza la densidad de bordes (surcos de la llanta).
    Muchos bordes paralelos → surcos profundos → llanta nueva
    Pocos bordes → desgaste avanzado
    """
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, threshold1=30, threshold2=100)
    edge_density = np.sum(edges > 0) / edges.size

    # Calibración: densidad < 2% = muy liso, densidad > 20% = surcos muy marcados
    score = np.interp(edge_density * 100, [1, 3, 8, 15, 25], [5, 20, 50, 80, 100])
    return float(score)


def _analyze_wear_pattern(roi: np.ndarray) -> tuple[str, float]:
    """
    Detecta el patrón de desgaste dividiendo la llanta en 3 zonas (izq, centro, der).
    Compara el nivel de desgaste en cada zona para identificar el patrón.
    """
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Dividir en 3 zonas horizontales
    left   = gray[:, :w//3]
    center = gray[:, w//3: 2*w//3]
    right  = gray[:, 2*w//3:]

    # Brillo promedio por zona (más brillo = más desgastado)
    bl = left.mean()
    bc = center.mean()
    br = right.mean()

    diff_threshold = 12  # diferencia significativa en brillo
    pattern = "uniform"
    confidence = 0.75

    if bc > bl + diff_threshold and bc > br + diff_threshold:
        pattern, confidence = "center", 0.82
    elif bl > bc + diff_threshold and br > bc + diff_threshold:
        pattern, confidence = "edge_both", 0.80
    elif bl > br + diff_threshold and bl > bc + diff_threshold:
        pattern, confidence = "edge_inner", 0.78
    elif br > bl + diff_threshold and br > bc + diff_threshold:
        pattern, confidence = "edge_outer", 0.78
    else:
        # Analizar varianza local para cupping/diagonal
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        local_var = lap.var()
        if local_var < 30:
            pattern, confidence = "uniform", 0.85
        elif local_var > 200:
            pattern, confidence = "cupping", 0.65  # varianza alta + irregular

    return pattern, confidence


def _detect_defects(img: np.ndarray, roi: np.ndarray) -> list[str]:
    """Detecta defectos visibles en la imagen."""
    defects = []
    gray_full = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray_roi  = cv2.cvtColor(roi,  cv2.COLOR_BGR2GRAY)

    # Detección de grietas: líneas finas en la goma
    edges = cv2.Canny(gray_full, 80, 200)
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=40, minLineLength=30, maxLineGap=5)
    if lines is not None and len(lines) > 25:
        defects.append("irregular_surface")

    # Zonas muy brillantes (posibles protuberancias o daño)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    bright_mask = hsv[:, :, 2] > 200
    bright_ratio = np.sum(bright_mask) / bright_mask.size
    if bright_ratio > 0.05:
        defects.append("reflective_damage")

    # Zona central muy lisa comparada con bordes
    roi_gray = gray_roi
    h, w = roi_gray.shape
    center_brightness = roi_gray[h//3:2*h//3, w//4:3*w//4].mean()
    edge_brightness   = np.concatenate([roi_gray[:, :w//8].flatten(), roi_gray[:, 7*w//8:].flatten()]).mean()
    if center_brightness > edge_brightness + 25:
        defects.append("center_wear")

    return defects


def _score_to_wear_level(score: int) -> tuple[str, float]:
    """Convierte score 0-100 a nivel de desgaste + confianza."""
    for level, info in WEAR_LEVELS.items():
        lo, hi = info["score_range"]
        if lo <= score <= hi:
            # Confianza más alta cuando el score está lejos de los bordes del rango
            margin = min(score - lo, hi - score)
            confidence = 0.70 + min(margin / (hi - lo) * 0.25, 0.25)
            return level, confidence
    return "medium", 0.60


def _score_to_depth(score: int) -> float:
    """Estima profundidad de surco en mm desde el score de condición."""
    # Interpolación: 100 → 8.0mm (nueva), 0 → 0.5mm (sin surco)
    return round(np.interp(score, [0, 20, 40, 65, 85, 100], [0.5, 1.5, 3.0, 5.0, 7.0, 8.0]), 1)


def _build_notes(wear_level: str, pattern: str, defects: list, tex: float, col: float) -> str:
    notes = []
    level_info = WEAR_LEVELS.get(wear_level, {})
    notes.append(f"Nivel: {level_info.get('label', wear_level)}")
    if pattern != "uniform":
        pattern_labels = {
            "center": "desgaste en zona central (inflado excesivo o alineación)",
            "edge_both": "desgaste en ambos bordes (inflado insuficiente)",
            "edge_inner": "desgaste en borde interior (problema de alineación/camber)",
            "edge_outer": "desgaste en borde exterior (sobrealimentación de curvas)",
            "cupping": "desgaste irregular tipo ondulado (amortiguadores o balanceo)",
            "diagonal": "desgaste diagonal (problemas de alineación)",
        }
        notes.append(f"Patrón: {pattern_labels.get(pattern, pattern)}")
    if defects:
        notes.append(f"Posibles defectos detectados: {', '.join(defects)}")
    return " | ".join(notes)


def _error_result(msg: str) -> TireAnalysisResult:
    return TireAnalysisResult(
        wear_level="unknown", confidence=0.0, condition_score=0,
        estimated_depth_mm=0.0, wear_pattern="uniform", pattern_confidence=0.0,
        defects=[], is_tire_detected=False, analysis_notes=msg,
    )
