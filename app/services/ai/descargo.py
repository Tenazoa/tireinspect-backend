"""
descargo.py — IA para bajas de llantas:

1) clasificar_dano(foto): Claude Vision identifica la CAUSA del daño usando la misma
   lista de causas del cuadro "CONDICIÓN ENCONTRADA" del SCRAK de TYMSAC.
2) generar_descargo(datos, foto): arma el Informe Técnico de Descargo en Word.
   El MONTO lo calcula el código (no la IA) con el método TYMSAC:
       pérdida = costo_nuevo × (cocada_al_retiro / cocada_original)
   Claude solo redacta el texto técnico.

Modelo configurable con DAMAGE_AI_MODEL (por defecto claude-opus-5).
"""
import base64
import io
import json
import os
from datetime import date

_MODEL = os.getenv("DAMAGE_AI_MODEL", "claude-opus-5")

CAUSAS = [
    "PESTAÑA DESGARRADA POR RECALENTAMIENTO",
    "CASCO SOPLADO INTERNAMENTE",
    "PASADA DE LÍMITE PARA REENCAUCHE",
    "ROTURA DE COSTADO POR GOLPES O PENETRACIONES",
    "IMPACTO EN EL COSTADO",
    "CORTES EN BANDA POR GOLPES O PENETRACIONES",
    "INCRUSTACIÓN DE OBJETO EN BANDA DE RODAMIENTO",
    "REPARACIÓN FUERA DE ESPECIFICACIÓN",
    "CASCO FATIGADO",
    "VENA EN EL COSTADO",
    "DEVUELTA DE REENCAUCHE POR PROBLEMAS DE CARCASA",
    "RODADA DESINFLADA",
    "HOMBRO SOPLADO",
    "OTRA",
]

_SCHEMA_DANO = {
    "type": "object",
    "properties": {
        "es_llanta": {"type": "boolean"},
        "causa": {"type": "string", "enum": CAUSAS},
        "descripcion_dano": {"type": "string"},
        "zona": {"type": "string", "enum": ["banda de rodamiento", "hombro", "costado", "pestaña", "interior", "no visible"]},
        "reparable": {"type": "boolean"},
        "reencauchable": {"type": "boolean"},
        "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        "notas": {"type": "string"},
    },
    "required": ["es_llanta", "causa", "descripcion_dano", "zona", "reparable", "reencauchable", "confianza", "notas"],
    "additionalProperties": False,
}

_PROMPT_DANO = """Eres un perito de neumáticos de camión de una flota de transporte pesado en Perú.
Mira la foto del neumático dado de baja y determina la CAUSA del daño eligiendo EXACTAMENTE una de la lista permitida.

Guía de causas:
- CASCO FATIGADO: carcasa deteriorada por uso normal / DOT antiguo (más de 5 años), grietas por envejecimiento.
- IMPACTO EN EL COSTADO / ROTURA DE COSTADO: golpe contra muro, piedra o bordillo que rompe el flanco.
- VENA EN EL COSTADO: abultamiento alargado en el flanco por cuerdas rotas.
- HOMBRO SOPLADO: separación/abultamiento en el hombro (zona entre banda y costado).
- CASCO SOPLADO INTERNAMENTE: separación interna de capas, ampolla general.
- RODADA DESINFLADA: marcas circulares o caucho molido en el flanco por rodar sin presión.
- PASADA DE LÍMITE PARA REENCAUCHE: banda gastada hasta mostrar lonas o alambres.
- CORTES EN BANDA / INCRUSTACIÓN DE OBJETO: cortes o un objeto clavado en la banda de rodamiento.
Si no se puede determinar con la foto, usa "OTRA" y explica en notas.
Escribe descripcion_dano en MAYÚSCULAS, estilo del informe (ej. "IMPACTO EN EL COSTADO CON DESGARRO DE LA CARCASA").
Contexto adicional de la llanta: %s"""


def _media_type(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _img_block(foto: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": _media_type(foto),
                                       "data": base64.standard_b64encode(foto).decode("utf-8")}}


def _cliente():
    import anthropic
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el servidor")
    return anthropic.Anthropic()


def _json_de(resp) -> dict:
    if resp.stop_reason == "refusal":
        raise RuntimeError("La IA no pudo analizar esta imagen")
    txt = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(txt)


def clasificar_dano(foto: bytes, contexto: str = "") -> dict:
    client = _cliente()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": [_img_block(foto), {"type": "text", "text": _PROMPT_DANO % (contexto or "sin datos")}]}],
        # salida JSON validada contra el esquema (la causa siempre sale de la lista)
        extra_body={"output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA_DANO}}},
    )
    return _json_de(resp)


# ── Informe de descargo ────────────────────────────────────────────────────

_SCHEMA_INF = {
    "type": "object",
    "properties": {
        "asunto": {"type": "string"},
        "antecedentes": {"type": "string"},
        "analisis_dano": {"type": "string"},
        "causa_raiz": {"type": "string"},
        "conclusiones": {"type": "array", "items": {"type": "string"}},
        "recomendaciones": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["asunto", "antecedentes", "analisis_dano", "causa_raiz", "conclusiones", "recomendaciones"],
    "additionalProperties": False,
}

_PROMPT_INF = """Redacta el texto de un INFORME TÉCNICO DE DESCARGO de neumático para TYMSAC (flota de transporte pesado, Perú),
en español formal y técnico, en tercera persona, sin inventar hechos que no estén en los datos.
Los montos ya están calculados: menciónalos tal cual, no los recalcules.

Datos del caso (JSON):
%s

Instrucciones:
- asunto: una línea, ej. "Informe de Descargo – Impacto en neumático 11R22.5 cód. 19076 de la unidad TEU-895".
- antecedentes: qué pasó, cuándo, dónde, unidad, conductor, posición (2-4 oraciones).
- analisis_dano: descripción técnica del daño%s y por qué el neumático queda fuera de servicio.
- causa_raiz: una o dos oraciones.
- conclusiones: 3-4 puntos (incluye si es reparable/reencauchable, el retiro prematuro con la cocada, y el monto a descontar).
- recomendaciones: 2-4 puntos prácticos."""


def monto_descargo(costo: float, cocada_orig: float, cocada_retiro: float) -> dict:
    """Método TYMSAC (informe de descargo): se cobra la cocada NO aprovechada."""
    por_mm = costo / cocada_orig
    monto = round(por_mm * cocada_retiro, 2)
    return {"costo_por_mm": round(por_mm, 2), "monto": monto,
            "pct_no_aprovechado": round(cocada_retiro / cocada_orig * 100, 1),
            "pct_aprovechado": round((cocada_orig - cocada_retiro) / cocada_orig * 100, 1)}


def generar_descargo(d: dict, foto: bytes | None = None) -> tuple[bytes, dict]:
    """d: codigo, placa, posicion, conductor, fecha, lugar, incidente, marca, modelo, medida, vida,
    costo, cocada_orig, cocada_retiro, km_recorrido. Devuelve (docx_bytes, resumen)."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    calc = monto_descargo(float(d["costo"]), float(d["cocada_orig"]), float(d["cocada_retiro"]))
    dano = None
    if foto:
        try:
            dano = clasificar_dano(foto, f"{d.get('marca','')} {d.get('medida','')} cód. {d.get('codigo','')}")
        except Exception:
            dano = None
    caso = {k: d.get(k) for k in ("codigo", "placa", "posicion", "conductor", "fecha", "lugar", "incidente",
                                   "marca", "modelo", "medida", "vida", "km_recorrido")}
    caso.update({"costo_soles_sin_igv": d["costo"], "cocada_original_mm": d["cocada_orig"],
                 "cocada_al_retiro_mm": d["cocada_retiro"], **{f"calc_{k}": v for k, v in calc.items()}})
    if dano:
        caso["analisis_foto_ia"] = dano

    client = _cliente()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": _PROMPT_INF % (
            json.dumps(caso, ensure_ascii=False, indent=1),
            " (apóyate en analisis_foto_ia)" if dano else "")}],
        extra_body={"output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA_INF}}},
    )
    t = _json_de(resp)

    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2.2)
    st = doc.styles["Normal"]; st.font.name = "Arial"; st.font.size = Pt(10.5)
    azul = RGBColor(0x1F, 0x3A, 0x68)

    def h(txt, size=12):
        p = doc.add_paragraph(); r = p.add_run(txt); r.bold = True; r.font.size = Pt(size); r.font.color.rgb = azul
        return p

    tp = doc.add_paragraph(); tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = tp.add_run("TYMSAC SOLUCIONES AL TRANSPORTE"); r.bold = True; r.font.size = Pt(14); r.font.color.rgb = azul
    tp = doc.add_paragraph(); tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = tp.add_run("INFORME TÉCNICO DE DESCARGO – NEUMÁTICOS"); r.bold = True; r.font.size = Pt(12)
    doc.add_paragraph(f"Asunto: {t['asunto']}")
    doc.add_paragraph(f"Fecha del informe: {date.today().strftime('%d/%m/%Y')}")

    h("1. DATOS DEL NEUMÁTICO Y DEL EVENTO")
    tab = doc.add_table(rows=0, cols=2); tab.style = "Light Grid Accent 1"
    for k, v in [("Código de llanta", d.get("codigo")), ("Unidad / Placa", d.get("placa")), ("Posición", d.get("posicion")),
                 ("Marca / Modelo", f"{d.get('marca') or ''} {d.get('modelo') or ''}".strip()), ("Medida", d.get("medida")),
                 ("Vida", d.get("vida")), ("Conductor", d.get("conductor")), ("Fecha del evento", d.get("fecha")),
                 ("Lugar", d.get("lugar")), ("Km recorridos", d.get("km_recorrido")),
                 ("Cocada original", f"{d['cocada_orig']} mm"), ("Cocada al retiro", f"{d['cocada_retiro']} mm")]:
        if v not in (None, "", " "):
            c = tab.add_row().cells; c[0].text = k; c[1].text = str(v)

    h("2. ANTECEDENTES"); doc.add_paragraph(t["antecedentes"])
    if foto:
        h("3. REGISTRO FOTOGRÁFICO")
        try:
            doc.add_picture(io.BytesIO(foto), width=Cm(11))
            cap = doc.add_paragraph("Foto del neumático dado de baja.")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        except Exception:
            doc.add_paragraph("(no se pudo insertar la foto)")
    h("4. ANÁLISIS DEL DAÑO"); doc.add_paragraph(t["analisis_dano"])
    if dano:
        doc.add_paragraph(f"Causa identificada (análisis IA de la foto, confianza {dano['confianza']}): {dano['causa']}. "
                          f"Zona: {dano['zona']}. Reparable: {'sí' if dano['reparable'] else 'no'}. "
                          f"Reencauchable: {'sí' if dano['reencauchable'] else 'no'}.")
    h("5. CAUSA RAÍZ"); doc.add_paragraph(t["causa_raiz"])

    h("6. CÁLCULO DEL MONTO A DESCONTAR")
    doc.add_paragraph("Monto = (Costo del neumático ÷ Cocada original) × Cocada al retiro (cocada no aprovechada)")
    tab = doc.add_table(rows=0, cols=2); tab.style = "Light Grid Accent 1"
    for k, v in [("Costo del neumático nuevo (sin IGV)", f"S/ {float(d['costo']):,.2f}"),
                 ("Costo por milímetro", f"S/ {calc['costo_por_mm']:,.2f} / mm"),
                 ("Cocada no aprovechada", f"{d['cocada_retiro']} mm ({calc['pct_no_aprovechado']}%)"),
                 ("MONTO A DESCONTAR (sin IGV)", f"S/ {calc['monto']:,.2f}")]:
        c = tab.add_row().cells; c[0].text = k; c[1].text = v
    tab.rows[-1].cells[1].paragraphs[0].runs[0].bold = True

    h("7. CONCLUSIONES")
    for x in t["conclusiones"]:
        doc.add_paragraph(x, style="List Bullet")
    h("8. RECOMENDACIONES")
    for x in t["recomendaciones"]:
        doc.add_paragraph(x, style="List Bullet")
    doc.add_paragraph("\n\n______________________________\nControl de Neumáticos – TYMSAC")

    buf = io.BytesIO(); doc.save(buf)
    return buf.getvalue(), {"monto": calc["monto"], "calculo": calc, "dano": dano, "asunto": t["asunto"]}
