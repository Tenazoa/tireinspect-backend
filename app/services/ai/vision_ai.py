"""
vision_ai.py — Análisis de llantas con IA de visión (Claude Vision).

Si existe la variable de entorno ANTHROPIC_API_KEY, envía la foto del neumático
a Claude y obtiene un análisis experto: cocada estimada, nivel de desgaste,
patrón, defectos (grietas, cortes, abultamientos, cordón expuesto, objetos
incrustados, desgaste irregular), código de fuego si es legible y recomendación.

Si NO hay clave o falla, devuelve None → el endpoint usa el análisis OpenCV.
"""
import os
import base64
import json
import re

_MODEL = os.getenv("TIRE_AI_MODEL", "claude-haiku-4-5-20251001")

_PROMPT = """Eres un inspector experto de neumáticos de camión (flota de transporte pesado, Perú).
Analiza LA FOTO del neumático y responde SOLO con un objeto JSON válido, sin texto adicional, con esta forma exacta:

{
  "es_llanta": true|false,
  "cocada_mm": number,            // profundidad estimada del surco en mm (0 a 24). Camión nuevo ~18-22, límite legal ~1.6-3
  "patron": "uniform"|"center"|"edge_inner"|"edge_outer"|"edge_both"|"cupping"|"diagonal",
  "defectos": [ ... ],           // de: "grieta","corte","abultamiento","cordon_expuesto","objeto_incrustado","desgaste_irregular","desprendimiento","separacion_banda". [] si no hay
  "codigo_fuego": string|null,   // SOLO el número/código GRABADO en el flanco (ej "19885"). NUNCA la marca ni la medida. Si no hay número legible: null
  "recomendacion": "ok"|"monitor"|"replace_soon"|"replace_now",
  "notas": string                // 1 frase corta en español para el inspector
}

Reglas:
- Si la imagen NO es un neumático, devuelve es_llanta=false y el resto en valores neutros.
- La recomendación debe ser COHERENTE con la cocada y los defectos: cocada<=3mm o defecto grave (grieta/corte/cordón/abultamiento/separación) => "replace_now".
- Sé conservador: ante duda de seguridad sube la recomendación.
- codigo_fuego: solo dígitos/código grabado; si ves la marca o la medida NO las pongas ahí (usa null).
- La cocada es una ESTIMACIÓN visual; si hay poca certeza dilo en notas.
Contexto de la llanta (puede ayudar): %s"""


def _nivel_from_cocada(mm: float) -> tuple[str, int]:
    """Nivel + score coherentes con la cocada (llanta de camión, máx ~22mm)."""
    if mm >= 14:   return "new", min(100, int(mm / 22 * 100))
    if mm >= 8:    return "low", 75
    if mm >= 5:    return "medium", 55
    if mm >= 3:    return "high", 32
    return "replace", max(5, int(mm / 3 * 15))


def _media_type(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def vision_diagnostic() -> dict:
    """Diagnóstico: por qué la visión no corre. Temporal, para depurar."""
    out = {"hasKey": bool(os.getenv("ANTHROPIC_API_KEY")), "model": _MODEL,
           "anthropicImport": False, "testCall": None, "error": None}
    try:
        import anthropic  # noqa
        out["anthropicImport"] = True
        out["anthropicVersion"] = getattr(anthropic, "__version__", "?")
    except Exception as e:
        out["error"] = f"import anthropic: {e}"
        return out
    if not out["hasKey"]:
        out["error"] = "Falta ANTHROPIC_API_KEY en el entorno"
        return out
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(model=_MODEL, max_tokens=10,
                                      messages=[{"role": "user", "content": "di OK"}])
        out["testCall"] = "".join(getattr(b, "text", "") for b in msg.content)[:50]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return out


def analyze_tire_vision(image_bytes: bytes, brand: str | None = None,
                        size: str | None = None, position: str | None = None) -> dict | None:
    """Devuelve dict normalizado o None si no hay clave / falla (→ fallback OpenCV)."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except Exception:
        return None

    ctx = ", ".join(x for x in [
        f"marca {brand}" if brand else "",
        f"medida {size}" if size else "",
        f"posición {position}" if position else "",
    ] if x) or "sin datos"

    try:
        client = anthropic.Anthropic(api_key=key)
        b64 = base64.standard_b64encode(image_bytes).decode()
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": _media_type(image_bytes), "data": b64}},
                    {"type": "text", "text": _PROMPT % ctx},
                ],
            }],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        data = _extract_json(text)
        if not data:
            return None

        PATRONES = {"uniform", "center", "edge_inner", "edge_outer", "edge_both", "cupping", "diagonal"}
        RECS = {"ok", "monitor", "replace_soon", "replace_now"}

        def fnum(v, default=0.0):
            try:
                return float(v)
            except Exception:
                return default

        cocada = max(0.0, min(24.0, fnum(data.get("cocada_mm"), 0.0)))
        # nivel + score SIEMPRE coherentes con la cocada (no confiamos en el modelo aquí)
        nivel, score = _nivel_from_cocada(cocada)

        patron = str(data.get("patron", "")).lower()
        if patron not in PATRONES:
            patron = "uniform"

        defectos = data.get("defectos") or []
        if not isinstance(defectos, list):
            defectos = []
        graves = {"grieta", "corte", "abultamiento", "cordon_expuesto", "separacion_banda", "desprendimiento"}
        tiene_grave = any(str(d).lower() in graves for d in defectos)

        rec = str(data.get("recomendacion", "")).lower()
        if rec not in RECS:
            rec = "monitor"
        # Coherencia: cocada crítica o defecto grave => cambio urgente
        if cocada <= 3 or tiene_grave:
            rec = "replace_now"

        # código de fuego: solo si es un código real (no la marca/medida)
        codigo = data.get("codigo_fuego")
        codigo = str(codigo).strip() if codigo else None
        if codigo:
            up = codigo.upper()
            bad = (brand and up == str(brand).upper()) or (size and up == str(size).upper()) \
                or not any(ch.isdigit() for ch in codigo) or len(codigo) > 20
            if bad:
                codigo = None

        return {
            "engine": "vision",
            "is_tire_detected": bool(data.get("es_llanta", True)),
            "wear_level": nivel,
            "condition_score": score,
            "estimated_depth_mm": round(cocada, 1),
            "wear_pattern": patron,
            "defects": [str(d) for d in defectos][:8],
            "fire_code": codigo,
            "recommendation": rec,
            "notes": str(data.get("notas", ""))[:300],
        }
    except Exception:
        return None
