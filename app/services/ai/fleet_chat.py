"""
fleet_chat.py — Asistente IA de la flota (chat con Claude + herramientas).

Claude responde preguntas en lenguaje natural sobre los datos de la empresa
usando herramientas de SOLO LECTURA (nunca modifica nada). Cada herramienta
filtra por company_id del usuario que pregunta.

Modelo configurable con CHAT_AI_MODEL (por defecto claude-opus-5).
"""
import json
import os
from collections import defaultdict

from sqlalchemy.orm import Session

from ...models.models import (TireSpec, TireStock, TireDetalle, TireMedida, TireKmVida,
                              VehicleKm, VehicleInfo, VehicleGastos)

_MODEL = os.getenv("CHAT_AI_MODEL", "claude-opus-5")
_MAX_TURNOS = 8            # tope de vueltas herramienta->respuesta por pregunta
_LIM = 50                  # filas maximas que devuelve una herramienta

SYSTEM = """Eres el asistente de neumáticos de TYMSAC / Llantacentro (flota de transporte pesado, Perú).
Respondes preguntas del equipo de control de neumáticos usando SOLO los datos que te dan las herramientas.

Contexto del negocio:
- Datos de SOLOMON: llantas montadas en unidades (por placa y posición), llantas en almacén/reencauche/ciclo final, historial de cocada (mm), km por unidad y mes, costos por unidad.
- Vida: 1V = primera vida (nueva), 1R/2R/3R = reencauches.
- Regla de desgaste de la empresa: 1 mm de cocada por cada 8,000 km que recorre la unidad; límite de retiro 3 mm.
- Montos en soles (S/).

Cómo responder:
- En español, claro y directo, para un supervisor de flota. Usa tablas cortas en markdown cuando ayuden.
- Si una herramienta devuelve pocos o ningún dato, dilo; nunca inventes cifras.
- Cuando des números, di de dónde salen (por ejemplo "según las llantas montadas registradas")."""

TOOLS = [
    {
        "name": "resumen_llantas",
        "description": ("Cuenta llantas agrupadas por un campo, con cocada promedio y cuántas están en 3 mm o menos. "
                        "Úsala para preguntas de totales, distribución por marca/medida/placa/vida o ubicación."),
        "input_schema": {
            "type": "object",
            "properties": {
                "conjunto": {"type": "string", "enum": ["montadas", "stock", "todas"],
                             "description": "montadas = en unidades; stock = almacén/reencauche/ciclo final/vendidas; todas = ambas"},
                "agrupar_por": {"type": "string", "enum": ["marca", "medida", "placa", "vida", "ubicacion"]},
                "marca": {"type": "string", "description": "filtro opcional (texto contenido, sin distinguir mayúsculas)"},
                "medida": {"type": "string", "description": "filtro opcional, ej. 11R22.5 o 295/80R22.5"},
                "placa": {"type": "string", "description": "filtro opcional"},
            },
            "required": ["conjunto", "agrupar_por"],
        },
    },
    {
        "name": "buscar_llantas",
        "description": ("Lista llantas individuales (máximo 50) con código, placa, posición, marca, modelo, medida, vida, "
                        "cocada, km y ubicación. Úsala para listar llantas de una placa, las de cocada baja, o buscar un código."),
        "input_schema": {
            "type": "object",
            "properties": {
                "conjunto": {"type": "string", "enum": ["montadas", "stock", "todas"]},
                "codigo": {"type": "string"},
                "placa": {"type": "string"},
                "marca": {"type": "string"},
                "medida": {"type": "string"},
                "cocada_max": {"type": "number", "description": "solo llantas con cocada <= este valor (mm)"},
                "cocada_min": {"type": "number"},
                "orden": {"type": "string", "enum": ["cocada_asc", "cocada_desc", "km_desc"]},
            },
            "required": ["conjunto"],
        },
    },
    {
        "name": "historial_llanta",
        "description": "Historial completo de una llanta por su código: estados en SOLOMON, mediciones de cocada en el tiempo y km por vida.",
        "input_schema": {
            "type": "object",
            "properties": {"codigo": {"type": "string"}},
            "required": ["codigo"],
        },
    },
    {
        "name": "km_unidad",
        "description": "Ficha de una unidad (marca, tipo, estado, km actual) y sus km recorridos por mes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "placa": {"type": "string"},
                "meses": {"type": "integer", "description": "cuántos meses recientes devolver (por defecto 12)"},
            },
            "required": ["placa"],
        },
    },
    {
        "name": "costos_unidades",
        "description": ("Ingreso y costos anuales por unidad (llantas, lubricantes, repuestos, taller). "
                        "Con placa devuelve sus años; sin placa devuelve el ranking de unidades por costo de llantas del año."),
        "input_schema": {
            "type": "object",
            "properties": {
                "placa": {"type": "string"},
                "anio": {"type": "integer"},
            },
        },
    },
]


def _like(v: str | None) -> str | None:
    v = (v or "").strip()
    return f"%{v}%" if v else None


def _norm_plate(p: str | None) -> str:
    return (p or "").replace("-", "").replace(" ", "").strip().upper()


def _rows_llantas(db: Session, cid: str, conjunto: str, codigo=None, placa=None, marca=None, medida=None):
    out = []
    if conjunto in ("montadas", "todas"):
        q = db.query(TireSpec).filter(TireSpec.company_id == cid)
        if codigo: q = q.filter(TireSpec.code == codigo.strip())
        if placa: q = q.filter(TireSpec.plate == _norm_plate(placa))
        if marca: q = q.filter(TireSpec.brand.ilike(_like(marca)))
        if medida: q = q.filter(TireSpec.size.ilike(_like(medida)))
        for s in q.all():
            out.append({"codigo": s.code, "placa": s.plate, "posicion": s.position, "marca": s.brand,
                        "modelo": s.model, "medida": s.size, "vida": s.life, "cocada": s.last_depth_mm,
                        "km_total": s.km_total, "km_vida": s.km_life, "estimado_km": s.estimado_km,
                        "ubicacion": "05. UNIDAD"})
    if conjunto in ("stock", "todas"):
        q = db.query(TireStock).filter(TireStock.company_id == cid)
        if codigo: q = q.filter(TireStock.code == codigo.strip())
        if placa: q = q.filter(TireStock.plate == _norm_plate(placa))
        if marca: q = q.filter(TireStock.brand.ilike(_like(marca)))
        if medida: q = q.filter(TireStock.size.ilike(_like(medida)))
        for s in q.all():
            out.append({"codigo": s.code, "placa": s.plate, "posicion": None, "marca": s.brand,
                        "modelo": s.model, "medida": s.size, "vida": s.life, "cocada": s.depth_mm,
                        "km_total": s.km_total, "km_vida": s.km_life, "estimado_km": s.estimado_km,
                        "ubicacion": s.ubicacion})
    return out


def _t_resumen(db, cid, a):
    rows = _rows_llantas(db, cid, a.get("conjunto", "montadas"), marca=a.get("marca"),
                         medida=a.get("medida"), placa=a.get("placa"))
    campo = {"marca": "marca", "medida": "medida", "placa": "placa", "vida": "vida",
             "ubicacion": "ubicacion"}[a.get("agrupar_por", "marca")]
    g = defaultdict(lambda: {"llantas": 0, "suma_cocada": 0.0, "con_cocada": 0, "en_3mm_o_menos": 0})
    for r in rows:
        k = r.get(campo) or "(sin dato)"
        e = g[k]; e["llantas"] += 1
        c = r.get("cocada")
        if c is not None:
            e["suma_cocada"] += c; e["con_cocada"] += 1
            if c <= 3: e["en_3mm_o_menos"] += 1
    res = sorted(({"grupo": k, "llantas": v["llantas"],
                   "cocada_promedio_mm": round(v["suma_cocada"] / v["con_cocada"], 1) if v["con_cocada"] else None,
                   "en_3mm_o_menos": v["en_3mm_o_menos"]} for k, v in g.items()),
                 key=lambda x: -x["llantas"])
    return {"total_llantas": len(rows), "grupos": len(res), "filas": res[:_LIM],
            "nota": f"se muestran los {_LIM} grupos mayores" if len(res) > _LIM else None}


def _t_buscar(db, cid, a):
    rows = _rows_llantas(db, cid, a.get("conjunto", "todas"), codigo=a.get("codigo"), placa=a.get("placa"),
                         marca=a.get("marca"), medida=a.get("medida"))
    if a.get("cocada_max") is not None:
        rows = [r for r in rows if r["cocada"] is not None and r["cocada"] <= a["cocada_max"]]
    if a.get("cocada_min") is not None:
        rows = [r for r in rows if r["cocada"] is not None and r["cocada"] >= a["cocada_min"]]
    orden = a.get("orden")
    if orden == "cocada_asc":
        rows.sort(key=lambda r: (r["cocada"] is None, r["cocada"] or 0))
    elif orden == "cocada_desc":
        rows.sort(key=lambda r: -(r["cocada"] or 0))
    elif orden == "km_desc":
        rows.sort(key=lambda r: -(r["km_total"] or 0))
    return {"encontradas": len(rows), "filas": rows[:_LIM],
            "nota": f"se muestran {_LIM} de {len(rows)}" if len(rows) > _LIM else None}


def _t_historial(db, cid, a):
    cod = (a.get("codigo") or "").strip()
    det = db.query(TireDetalle).filter(TireDetalle.company_id == cid, TireDetalle.code == cod).all()
    claves = ("TipoEstado", "nCicloVida", "CocadaPrimera", "CocadaActual", "Marca", "Modelo", "Medida", "Placa",
              "PosicionActual", "FechaIngreso", "FUltMedida", "FechaFin", "ObsSal", "KmMinimo", "Chofer")
    estados = [{k: (d.data or {}).get(k) for k in claves if (d.data or {}).get(k) not in (None, "")} for d in det]
    med = (db.query(TireMedida).filter(TireMedida.company_id == cid, TireMedida.code == cod)
           .order_by(TireMedida.fecha).all())
    kmv = db.query(TireKmVida).filter(TireKmVida.company_id == cid, TireKmVida.code == cod).all()
    actual = _rows_llantas(db, cid, "todas", codigo=cod)
    if not (det or med or kmv or actual):
        return {"error": f"No hay datos de la llanta {cod}"}
    return {"codigo": cod, "situacion_actual": actual, "registros_solomon": estados[:20],
            "mediciones_cocada": [{"fecha": m.fecha, "cocada_mm": m.cocada} for m in med][-40:],
            "km_por_vida": [{"vida": k.vida, "km": k.km_vida} for k in kmv]}


def _t_km(db, cid, a):
    p = _norm_plate(a.get("placa"))
    info = db.query(VehicleInfo).filter(VehicleInfo.company_id == cid, VehicleInfo.plate == p).first()
    km = (db.query(VehicleKm).filter(VehicleKm.company_id == cid, VehicleKm.plate == p)
          .order_by(VehicleKm.year.desc(), VehicleKm.month.desc()).limit(int(a.get("meses") or 12)).all())
    if not info and not km:
        return {"error": f"No hay datos de la placa {p}"}
    ficha = None
    if info:
        ficha = {"placa": info.plate, "marca": info.marca, "modelo": info.modelo, "tipo": info.tipo,
                 "estado": info.estado, "activo": info.activo, "km_actual": info.km_actual}
    return {"ficha": ficha, "km_por_mes": [{"anio": k.year, "mes": k.month, "km": k.km} for k in km]}


def _t_costos(db, cid, a):
    q = db.query(VehicleGastos).filter(VehicleGastos.company_id == cid)
    if a.get("placa"):
        q = q.filter(VehicleGastos.plate == _norm_plate(a["placa"]))
    if a.get("anio"):
        q = q.filter(VehicleGastos.anio == int(a["anio"]))
    rows = [{"placa": g.plate, "anio": g.anio, "tipo_unidad": g.tipo_unidad, "ingreso": g.ingreso,
             "costo_llantas": g.costo_llantas, "lubricantes": g.lubricantes, "repuestos": g.repuestos,
             "taller": g.taller, "otros": g.otros} for g in q.all()]
    rows.sort(key=lambda r: -(r["costo_llantas"] or 0))
    return {"filas": rows[:_LIM], "total_filas": len(rows)}


_HANDLERS = {"resumen_llantas": _t_resumen, "buscar_llantas": _t_buscar, "historial_llanta": _t_historial,
             "km_unidad": _t_km, "costos_unidades": _t_costos}


def _ejecutar(db: Session, cid: str, nombre: str, entrada: dict) -> tuple[str, bool]:
    fn = _HANDLERS.get(nombre)
    if not fn:
        return f"Herramienta desconocida: {nombre}", True
    try:
        return json.dumps(fn(db, cid, entrada or {}), ensure_ascii=False, default=str), False
    except Exception as e:  # el error vuelve a Claude como tool_result con is_error
        return f"Error al consultar: {e}", True


def responder(db: Session, company_id: str, historial: list[dict]) -> dict:
    """historial = [{"role": "user"|"assistant", "content": "texto"}, ...] (el último es la pregunta).
    Devuelve {"respuesta": str, "herramientas": [nombres usados]}."""
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"respuesta": "El asistente IA no está configurado (falta ANTHROPIC_API_KEY en el servidor).",
                "herramientas": []}
    client = anthropic.Anthropic()
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial[-20:]
                if m.get("role") in ("user", "assistant") and m.get("content")]
    usadas: list[str] = []
    resp = None
    for _ in range(_MAX_TURNOS):
        resp = client.beta.messages.create(
            model=_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=mensajes,
            betas=["server-side-fallback-2026-07-01"],
            # effort medio: preguntas de consulta, no requieren razonamiento largo.
            # fallbacks "default": si Claude declina, el servidor reintenta en el modelo recomendado.
            # (por extra_body para no depender de la version exacta del SDK 0.x instalada en Render)
            extra_body={"output_config": {"effort": "medium"}, "fallbacks": "default"},
        )
        if resp.stop_reason == "refusal":
            return {"respuesta": "No puedo responder esa pregunta. Intenta reformularla sobre los datos de la flota.",
                    "herramientas": usadas}
        if resp.stop_reason != "tool_use":
            break
        mensajes.append({"role": "assistant", "content": resp.content})
        resultados = []
        for b in resp.content:
            if b.type == "tool_use":
                usadas.append(b.name)
                texto, es_error = _ejecutar(db, company_id, b.name, b.input)
                resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": texto,
                                   "is_error": es_error})
        mensajes.append({"role": "user", "content": resultados})   # todos los resultados en un solo mensaje
    texto = "\n".join(b.text for b in (resp.content if resp else []) if b.type == "text").strip()
    if resp is not None and resp.stop_reason == "tool_use":
        texto = texto or "La consulta necesitó demasiados pasos. Intenta una pregunta más específica."
    return {"respuesta": texto or "(sin respuesta)", "herramientas": usadas}
