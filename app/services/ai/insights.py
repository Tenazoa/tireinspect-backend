"""
Insights con IA sobre datos ya calculados (snapshots de analítica):
  - ask_gastos: responde preguntas en lenguaje natural sobre el gasto en ruta
    de reparación de llantas (parte 'gastos_ruta').
  - alertas_desgaste: convierte la predicción de desgaste (parte 'pred') en
    alertas accionables en lenguaje claro.

Usa la clave ANTHROPIC_API_KEY del servidor. Modelo barato por defecto (Haiku),
configurable con INSIGHTS_AI_MODEL.
"""
import os
import json
from sqlalchemy.orm import Session

_MODEL = os.getenv("INSIGHTS_AI_MODEL", "claude-haiku-4-5-20251001")
RUBRO_LL = "REP. DE LLANTAS"


def _load(db: Session, company_id, parte: str):
    from ...models.models import AnaliticaSnapshot
    s = (db.query(AnaliticaSnapshot)
         .filter(AnaliticaSnapshot.company_id == company_id, AnaliticaSnapshot.parte == parte)
         .first())
    return s.data if (s and s.data is not None) else None


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _texto(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# ── #4: Pregúntale a tus gastos ──────────────────────────────────────────────

_SYS_GASTOS = (
    "Eres el analista de gastos de llantas de una flota de transporte peruana (TYMSAC). "
    "Respondes SOLO con los datos JSON que te doy (gasto en ruta por reparación de llantas). "
    "Montos en soles (S/), redondea a 2 decimales. Sé breve, directo y usa viñetas cuando ayude. "
    "Si la pregunta no se puede responder con estos datos, dilo claramente. No inventes cifras."
)


def ask_gastos(db: Session, company_id, pregunta: str) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "respuesta": "El asistente no está configurado (falta ANTHROPIC_API_KEY en el servidor)."}
    pregunta = (pregunta or "").strip()
    if not pregunta:
        return {"ok": False, "respuesta": "Escribe una pregunta."}
    data = _load(db, company_id, "gastos_ruta")
    filas = [f for f in ((data or {}).get("filas") or []) if str(f.get("Rubro")) == RUBRO_LL]
    if not filas:
        return {"ok": False, "respuesta": "Aún no hay datos de gastos en ruta cargados en la web."}

    def _f(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    por_cond, por_placa, por_mes, por_anio, reincid = {}, {}, {}, {}, {}
    for f in filas:
        c = str(f.get("Conductor") or "—"); pl = str(f.get("Tracto") or "—")
        mes = str(f.get("Mes") or ""); an = mes[:4]
        cod = str(f.get("CodigoLlanta") or "").strip()
        m = _f(f.get("Monto"))
        for d, k in ((por_cond, c), (por_placa, pl), (por_mes, mes), (por_anio, an)):
            b = d.setdefault(k, {"total": 0.0, "vales": 0}); b["total"] += m; b["vales"] += 1
        if cod:
            b = reincid.setdefault(cod, {"reparaciones": 0, "monto": 0.0, "placas": set()})
            b["reparaciones"] += 1; b["monto"] += m
            if f.get("Tracto"):
                b["placas"].add(str(f.get("Tracto")))

    def _top(d, n=40):
        return [{"nombre": k, "total": round(v["total"], 2), "vales": v["vales"]}
                for k, v in sorted(d.items(), key=lambda kv: kv[1]["total"], reverse=True)[:n]]

    repetidas = sorted(
        ({"codigo": k, "reparaciones": v["reparaciones"], "monto": round(v["monto"], 2),
          "placas": sorted(v["placas"])} for k, v in reincid.items() if v["reparaciones"] >= 2),
        key=lambda x: x["reparaciones"], reverse=True)[:40]

    contexto = {
        "moneda": "PEN (S/)",
        "rubro": "solo reparación de llantas en ruta",
        "total": round(sum(_f(f.get("Monto")) for f in filas), 2),
        "n_vales": len(filas),
        "por_conductor": _top(por_cond),
        "por_placa": _top(por_placa),
        "por_mes": [{"mes": k, "total": round(v["total"], 2), "vales": v["vales"]}
                    for k, v in sorted(por_mes.items())],
        "por_anio": [{"anio": k, "total": round(v["total"], 2), "vales": v["vales"]}
                     for k, v in sorted(por_anio.items())],
        "llantas_reincidentes": repetidas,
    }
    try:
        resp = _client().messages.create(
            model=_MODEL, max_tokens=1200, system=_SYS_GASTOS,
            messages=[{"role": "user", "content":
                       f"DATOS (JSON):\n{json.dumps(contexto, ensure_ascii=False)}\n\nPREGUNTA: {pregunta}"}],
        )
        return {"ok": True, "respuesta": _texto(resp)}
    except Exception as e:
        return {"ok": False, "respuesta": f"No se pudo consultar la IA: {str(e)[:160]}"}


# ── #3: Alertas de desgaste en lenguaje claro ────────────────────────────────

_SYS_ALERTAS = (
    "Eres el jefe de control de neumáticos de una flota peruana (TYMSAC). "
    "Recibes la predicción de desgaste de llantas (regla: se llega al límite de retiro según km recorridos). "
    "Escribe alertas ACCIONABLES en español claro, ordenadas por urgencia (lo más urgente primero). "
    "Formato Markdown: agrupa por urgencia con encabezados, y por cada llanta una viñeta corta tipo "
    "'Placa X · llanta CÓDIGO (posición) — cambiar/reencauchar en ~N semanas (retiro aprox. FECHA)'. "
    "Cierra con 1-2 frases de recomendación general. Sé conciso; no inventes datos que no estén en el JSON."
)


def alertas_desgaste(db: Session, company_id, horizonte: str | None = None, limite: int = 60) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "texto": "El asistente no está configurado (falta ANTHROPIC_API_KEY en el servidor)."}
    data = _load(db, company_id, "pred")
    if not data:
        return {"ok": False, "texto": "Aún no hay predicción de desgaste cargada en la web."}
    rows = [r for r in data if (not horizonte or str(r.get("Horizonte")) == horizonte)]
    # Priorizar por fecha de retiro más cercana / menor cocada.
    def _key(r):
        return (str(r.get("FechaRetiro") or "9999"), _num(r.get("CocadaHoy")))
    rows = sorted(rows, key=_key)[:limite]
    if not rows:
        return {"ok": True, "texto": "Sin llantas en alerta para ese horizonte. 👍", "n": 0}

    compact = [{
        "placa": r.get("Placa"), "llanta": r.get("Codigo"), "posicion": r.get("Posicion"),
        "marca": r.get("Marca"), "medida": r.get("Medida"), "vida": r.get("Vida"),
        "cocada_hoy": r.get("CocadaHoy"), "km_dia": r.get("KmDia"),
        "fecha_retiro": r.get("FechaRetiro"), "cuando": r.get("Horizonte"),
        "sugerencia": r.get("Sugerencia"),
    } for r in rows]
    try:
        resp = _client().messages.create(
            model=_MODEL, max_tokens=1600, system=_SYS_ALERTAS,
            messages=[{"role": "user", "content":
                       f"PREDICCIÓN (JSON, {len(compact)} llantas):\n{json.dumps(compact, ensure_ascii=False)}\n\n"
                       "Redacta las alertas accionables."}],
        )
        return {"ok": True, "texto": _texto(resp), "n": len(compact)}
    except Exception as e:
        return {"ok": False, "texto": f"No se pudo consultar la IA: {str(e)[:160]}"}


def _num(x):
    try:
        return float(x)
    except Exception:
        return 99.0


# ── Resumen ejecutivo de alto impacto (cuantitativo + cualitativo) ───────────

_SYS_EJEC = (
    "Eres el gerente de control de neumáticos de TYMSAC (transporte de carga en Perú) y escribes "
    "para la Gerencia General. Con las MÉTRICAS JSON que te doy redacta un informe ejecutivo de ALTO "
    "IMPACTO, cuantitativo y cualitativo, en español profesional y directo. Usa Markdown con estas "
    "secciones EXACTAS y breves:\n"
    "## Situación general\n(2-3 frases con las cifras clave de la flota y actividad)\n"
    "## Costos y ahorro\n(viñetas con cifras en S/: gasto en reparación de llantas en ruta, ahorro por "
    "reencauche, scrap/pérdida, costo de compra inmediata si lo hay)\n"
    "## Riesgos y hallazgos\n(viñetas: llantas por cambiar/vencidas, unidades inactivas, llantas que se "
    "reparan repetidamente, marcas de bajo rendimiento)\n"
    "## Recomendaciones\n(3 a 5 acciones priorizadas y concretas, cada una con su impacto esperado)\n"
    "Cita SOLO cifras que estén en el JSON; si un dato falta, omítelo. Redondea soles a enteros. "
    "Sé conciso: máximo ~250 palabras."
)


def resumen_ejecutivo(db: Session, company_id) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"ok": False, "texto": "El resumen con IA no está configurado (falta ANTHROPIC_API_KEY en el servidor).", "metricas": {}}
    from ...models.models import Vehicle
    m: dict = {"moneda": "PEN (S/)"}

    # Flota (conteo real por tipo/estado)
    vehs = db.query(Vehicle).filter(Vehicle.company_id == company_id).all()
    tmap = {"truck": "Tractos", "trailer": "Carretas", "car": "Camionetas"}
    flota = {"total": len(vehs), "activas": sum(1 for v in vehs if getattr(v, "active", True)),
             "inactivas": sum(1 for v in vehs if getattr(v, "active", True) is False), "por_tipo": {}}
    for v in vehs:
        k = tmap.get(v.type, v.type or "otros")
        b = flota["por_tipo"].setdefault(k, {"total": 0, "activas": 0})
        b["total"] += 1
        if getattr(v, "active", True):
            b["activas"] += 1
    m["flota"] = flota

    # Gasto en reparación de llantas en ruta (snapshot gastos_ruta)
    g = _load(db, company_id, "gastos_ruta") or {}
    filas = [f for f in (g.get("filas") or []) if str(f.get("Rubro")) == RUBRO_LL]
    if filas:
        def _f(x):
            try:
                return float(x or 0)
            except Exception:
                return 0.0
        by_c: dict = {}
        reinc = 0
        seen: dict = {}
        for f in filas:
            by_c[str(f.get("Conductor") or "—")] = by_c.get(str(f.get("Conductor") or "—"), 0.0) + _f(f.get("Monto"))
            cod = str(f.get("CodigoLlanta") or "").strip()
            if cod:
                seen[cod] = seen.get(cod, 0) + 1
        reinc = sum(1 for c in seen.values() if c >= 3)
        top = max(by_c.items(), key=lambda kv: kv[1]) if by_c else (None, 0)
        m["gasto_reparacion_llantas_ruta"] = {
            "total": round(sum(_f(f.get("Monto")) for f in filas), 2), "n_vales": len(filas),
            "conductor_top": top[0], "conductor_top_monto": round(top[1], 2),
            "llantas_reparadas_3mas": reinc,
        }

    # Predicción de retiro (snapshot pred)
    pred = _load(db, company_id, "pred") or []
    if pred:
        cnt: dict = {}
        for r in pred:
            cnt[str(r.get("Horizonte"))] = cnt.get(str(r.get("Horizonte")), 0) + 1
        m["prediccion"] = {"total_por_cambiar": len(pred), "por_horizonte": cnt,
                           "vencidas": cnt.get("VENCIDA", 0), "en_30_dias": cnt.get("0-30 DIAS", 0)}

    # Reencauche (snapshot reencauche): conteo por decisión
    reenc = _load(db, company_id, "reencauche") or []
    if reenc:
        dec: dict = {}
        for r in reenc:
            dec[str(r.get("Decision"))] = dec.get(str(r.get("Decision")), 0) + 1
        m["reencauche"] = {"total": len(reenc), "por_decision": dec}

    # Bajas / scrap (snapshot bajas + gerencial)
    bajas = _load(db, company_id, "bajas") or []
    if bajas:
        m["bajas"] = {"total": len(bajas)}
    ger = _load(db, company_id, "gerencial") or {}
    if ger:
        m["scrap"] = {"mes": ger.get("scrap_mes"), "anio": ger.get("scrap_2026"),
                      "alertas_altas": ger.get("alertas_altas")}

    try:
        resp = _client().messages.create(
            model=_MODEL, max_tokens=1600, system=_SYS_EJEC,
            messages=[{"role": "user", "content": f"MÉTRICAS (JSON):\n{json.dumps(m, ensure_ascii=False)}"}],
        )
        return {"ok": True, "texto": _texto(resp), "metricas": m}
    except Exception as e:
        return {"ok": False, "texto": f"No se pudo generar el resumen con IA: {str(e)[:160]}", "metricas": m}
