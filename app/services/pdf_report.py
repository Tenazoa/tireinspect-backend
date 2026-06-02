"""
Generación de reportes PDF de inspección — Fase 4.

Produce un PDF profesional por inspección con:
  - Cabecera con datos de empresa, vehículo e inspector
  - Tabla de llantas con profundidad, marca, recomendación
  - Resumen con semáforo de estado
  - Pie con firma y fecha
"""

import io
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)


REC_LABEL = {
    "ok": "OK", "monitor": "Vigilar",
    "replace_soon": "Cambio próximo", "replace_now": "CAMBIO URGENTE",
}
REC_COLOR = {
    "ok": colors.HexColor("#3fb950"),
    "monitor": colors.HexColor("#d29922"),
    "replace_soon": colors.HexColor("#f78166"),
    "replace_now": colors.HexColor("#e94560"),
}
import re

_LEGACY_POS = {
    "FL": "Direccional Izq.", "FR": "Direccional Der.",
    "RL": "Trasera Izq.", "RR": "Trasera Der.",
    "RL2": "Trasera Izq. 2", "RR2": "Trasera Der. 2",
    "RL3": "Trasera Izq. 3", "RR3": "Trasera Der. 3",
}


def position_label(code: str) -> str:
    """Etiqueta legible para cualquier código de posición (duales, repuestos, legado)."""
    if code in _LEGACY_POS:
        return _LEGACY_POS[code]
    if code.startswith("SP"):
        return f"Repuesto {code[2:]}"
    m = re.match(r"^A(\d)([LR])([OI])$", code)
    if m:
        axle, side, pos = m.groups()
        lado = "Izq." if side == "L" else "Der."
        ext = "Ext." if pos == "O" else "Int."
        return f"Eje {axle} {lado} {ext}"
    return code


# Compat: dict-like acceso usado en el resto del módulo
class _PosLabel:
    def get(self, code, default=None):
        label = position_label(code)
        return label if label != code else (default if default is not None else code)
    def __getitem__(self, code):
        return position_label(code)

POSITION_LABEL = _PosLabel()


def generate_inspection_pdf(inspection, vehicle, inspector, company_name: str) -> bytes:
    """
    Genera el PDF de una inspección. Retorna los bytes del archivo.
    `inspection`, `vehicle`, `inspector` son objetos ORM.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleX", parent=styles["Title"], fontSize=20,
        textColor=colors.HexColor("#1a1a2e"), spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=10,
        textColor=colors.HexColor("#666666"),
    )
    h2 = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontSize=12,
        textColor=colors.HexColor("#1a1a2e"), spaceBefore=12, spaceAfter=6,
    )

    elements = []

    # ── Cabecera ──
    elements.append(Paragraph("TireInspect — Reporte de Inspección", title_style))
    elements.append(Paragraph(company_name, sub_style))
    elements.append(Spacer(1, 8 * mm))

    # ── Datos generales ──
    created = inspection.completed_at or inspection.created_at
    fecha = created.strftime("%d/%m/%Y %H:%M") if created else "—"
    info_data = [
        ["Placa:", vehicle.plate, "Fecha:", fecha],
        ["Vehículo:", f"{vehicle.brand} {vehicle.model} {vehicle.year or ''}", "Inspector:", inspector.name],
        ["Tipo:", (vehicle.type or "").capitalize(), "Odómetro:", f"{inspection.odometer_km or '—'} km"],
    ]
    info_table = Table(info_data, colWidths=[25 * mm, 65 * mm, 25 * mm, 59 * mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#333333")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#cccccc")),
    ]))
    elements.append(info_table)

    # ── Resumen de estado ──
    tires = list(inspection.tires)
    counts = {"ok": 0, "monitor": 0, "replace_soon": 0, "replace_now": 0}
    for t in tires:
        counts[t.recommendation] = counts.get(t.recommendation, 0) + 1

    elements.append(Paragraph("Resumen", h2))
    summary_data = [[
        f"OK: {counts['ok']}",
        f"Vigilar: {counts['monitor']}",
        f"Cambio próximo: {counts['replace_soon']}",
        f"Urgente: {counts['replace_now']}",
    ]]
    summary_table = Table(summary_data, colWidths=[43.5 * mm] * 4)
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#e8f5e9")),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#fff8e1")),
        ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#fff3e0")),
        ("BACKGROUND", (3, 0), (3, 0), colors.HexColor("#ffebee")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    elements.append(summary_table)

    # ── Detalle por llanta ──
    elements.append(Paragraph("Detalle por posición", h2))
    header = ["Posición", "Profundidad", "Marca / Modelo", "Medida", "Presión", "Estado"]
    rows = [header]
    for t in tires:
        depth = (
            f"{t.tread_depth_center:.1f} mm" if t.tread_depth_center is not None else "—"
        )
        marca = " ".join(filter(None, [t.brand, t.model])) or "—"
        rows.append([
            POSITION_LABEL.get(t.position, t.position),
            depth,
            marca,
            t.size or "—",
            f"{t.pressure_psi:.0f} PSI" if t.pressure_psi else "—",
            REC_LABEL.get(t.recommendation, t.recommendation),
        ])

    tire_table = Table(rows, colWidths=[28 * mm, 24 * mm, 45 * mm, 28 * mm, 20 * mm, 29 * mm])
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    # Colorear celda de estado según recomendación
    for i, t in enumerate(tires, start=1):
        style.append(("TEXTCOLOR", (5, i), (5, i), REC_COLOR.get(t.recommendation, colors.black)))
        style.append(("FONTNAME", (5, i), (5, i), "Helvetica-Bold"))
    tire_table.setStyle(TableStyle(style))
    elements.append(tire_table)

    # ── Notas ──
    notas = [t for t in tires if t.notes]
    if notas:
        elements.append(Paragraph("Observaciones", h2))
        for t in notas:
            elements.append(Paragraph(
                f"<b>{POSITION_LABEL.get(t.position, t.position)}:</b> {t.notes}",
                sub_style,
            ))

    # ── Pie ──
    elements.append(Spacer(1, 16 * mm))
    footer = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8,
                            textColor=colors.HexColor("#999999"), alignment=1)
    elements.append(Paragraph(
        f"Reporte generado por TireInspect el "
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')} — Inspector: {inspector.name}",
        footer,
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer.read()
