"""
Auto-seed idempotente: crea empresa, inspector demo y vehículos de ejemplo
solo si la base de datos está vacía. Se ejecuta al arrancar (útil en la nube).
Además carga la flota real de SOLOMON (fleet_seed.json) para que los 260
vehículos persistan aunque Render reinicie con SQLite efímera.
"""
import uuid
import os
import json
from sqlalchemy.orm import Session
from .database import SessionLocal
from .security import hash_password
from ..models.models import Company, Inspector, Vehicle, TireSpec

TRUCK_6X4 = ["FL", "FR", "A2LO", "A2LI", "A2RI", "A2RO", "A3LO", "A3LI", "A3RI", "A3RO"]
TRAILER_6X0 = ["A1LO", "A1LI", "A1RI", "A1RO", "A2LO", "A2LI", "A2RI", "A2RO", "A3LO", "A3LI", "A3RI", "A3RO", "SP1", "SP2"]

_FLEET_JSON = os.path.join(os.path.dirname(__file__), "fleet_seed.json")


def _infer_type(solomon_type, n):
    t = (solomon_type or "").upper()
    if "CARRETA" in t or "SEMI" in t or "REMOLQ" in t:
        return "trailer"
    return "truck"


def _load_fleet(db: Session, company_id: str) -> int:
    """Carga la flota real de SOLOMON desde fleet_seed.json."""
    if not os.path.exists(_FLEET_JSON):
        return 0
    try:
        with open(_FLEET_JSON, encoding="utf-8") as fh:
            fleet = json.load(fh)
    except Exception as e:
        print(f"[seed] No se pudo leer fleet_seed.json: {e}")
        return 0

    count = 0
    for v in fleet:
        plate = str(v.get("plate", "")).strip().upper()
        if not plate:
            continue
        tires = v.get("tires", [])
        positions = [t.get("position") for t in tires if t.get("position")]
        vtype = _infer_type(v.get("type"), len(positions))
        db.add(Vehicle(
            id=str(uuid.uuid4()), plate=plate, brand="—", model="—",
            type=vtype, axle_count=3, tire_positions=positions, company_id=company_id,
        ))
        for t in tires:
            db.add(TireSpec(
                id=str(uuid.uuid4()), plate=plate, position=t.get("position"),
                brand=t.get("brand"), model=t.get("model"), size=t.get("size"),
                last_depth_mm=t.get("lastDepthMm"), code=t.get("code"),
                life=t.get("life"), vehicle_type=vtype, company_id=company_id,
            ))
        count += 1
    return count


def seed_if_empty() -> None:
    db: Session = SessionLocal()
    try:
        if db.query(Inspector).first():
            return  # ya hay datos, no hacer nada

        company = Company(id=str(uuid.uuid4()), name="Flota Demo SA")
        db.add(company)
        db.flush()

        inspector = Inspector(
            id=str(uuid.uuid4()),
            name="Juan Inspector",
            email="inspector@demo.com",
            hashed_password=hash_password("demo1234"),
            role="inspector",
            company_id=company.id,
        )
        db.add(inspector)

        # Solo flota real de SOLOMON (sin placas demo)
        fleet_count = _load_fleet(db, company.id)

        db.commit()
        print(f"[seed] Inspector + {fleet_count} vehiculos SOLOMON creados: inspector@demo.com / demo1234")
    except Exception as e:
        db.rollback()
        print(f"[seed] Error: {e}")
    finally:
        db.close()
