"""
Auto-seed idempotente: crea empresa, inspector demo y vehículos de ejemplo
solo si la base de datos está vacía. Se ejecuta al arrancar (útil en la nube).
"""
import uuid
from sqlalchemy.orm import Session
from .database import SessionLocal
from .security import hash_password
from ..models.models import Company, Inspector, Vehicle

TRUCK_6X4 = ["FL", "FR", "A2LO", "A2LI", "A2RI", "A2RO", "A3LO", "A3LI", "A3RI", "A3RO"]
TRAILER_6X0 = ["A1LO", "A1LI", "A1RI", "A1RO", "A2LO", "A2LI", "A2RI", "A2RO", "A3LO", "A3LI", "A3RI", "A3RO", "SP1", "SP2"]


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

        vehicles = [
            {"plate": "ABC-123", "brand": "Volvo", "model": "FH 460", "year": 2021, "type": "truck", "axle_count": 3, "tire_positions": TRUCK_6X4},
            {"plate": "XYZ-789", "brand": "Kenworth", "model": "T680", "year": 2019, "type": "truck", "axle_count": 3, "tire_positions": TRUCK_6X4},
            {"plate": "DEF-456", "brand": "Mercedes-Benz", "model": "Actros", "year": 2022, "type": "truck", "axle_count": 3, "tire_positions": TRUCK_6X4},
            {"plate": "TR1-001", "brand": "Randon", "model": "Carreta 3 ejes", "year": 2020, "type": "trailer", "axle_count": 3, "tire_positions": TRAILER_6X0},
        ]
        for vd in vehicles:
            db.add(Vehicle(id=str(uuid.uuid4()), company_id=company.id, **vd))

        db.commit()
        print("[seed] Datos demo creados: inspector@demo.com / demo1234")
    except Exception as e:
        db.rollback()
        print(f"[seed] Error: {e}")
    finally:
        db.close()
