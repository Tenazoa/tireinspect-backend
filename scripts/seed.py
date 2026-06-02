"""
Seed inicial: crea empresa, inspector y vehículos de prueba.
Uso: python -m scripts.seed
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app.core.database import SessionLocal, Base, engine
from app.core.security import hash_password
from app.models.models import Company, Inspector, Vehicle
import uuid

Base.metadata.create_all(bind=engine)

db = SessionLocal()

# Empresa
company = db.query(Company).filter(Company.name == "Flota Demo SA").first()
if not company:
    company = Company(id=str(uuid.uuid4()), name="Flota Demo SA")
    db.add(company)
    db.flush()

# Inspector
inspector = db.query(Inspector).filter(Inspector.email == "inspector@demo.com").first()
if not inspector:
    inspector = Inspector(
        id=str(uuid.uuid4()),
        name="Juan Inspector",
        email="inspector@demo.com",
        hashed_password=hash_password("demo1234"),
        role="inspector",
        company_id=company.id,
    )
    db.add(inspector)

# Vehículos de prueba
vehicles = [
    {"plate": "ABC-123", "brand": "Volvo", "model": "FH 460", "year": 2021, "type": "truck",
     "axle_count": 3, "tire_positions": ["FL", "FR", "RL", "RR", "RL2", "RR2"]},
    {"plate": "XYZ-789", "brand": "Kenworth", "model": "T680", "year": 2019, "type": "truck",
     "axle_count": 3, "tire_positions": ["FL", "FR", "RL", "RR", "RL2", "RR2"]},
    {"plate": "DEF-456", "brand": "Mercedes-Benz", "model": "Sprinter", "year": 2022, "type": "van",
     "axle_count": 2, "tire_positions": ["FL", "FR", "RL", "RR"]},
    {"plate": "GHI-321", "brand": "Scania", "model": "R450", "year": 2020, "type": "truck",
     "axle_count": 3, "tire_positions": ["FL", "FR", "RL", "RR", "RL2", "RR2"]},
]

for v_data in vehicles:
    existing = db.query(Vehicle).filter(Vehicle.plate == v_data["plate"]).first()
    if not existing:
        vehicle = Vehicle(
            id=str(uuid.uuid4()),
            company_id=company.id,
            **v_data
        )
        db.add(vehicle)

db.commit()
db.close()

print("OK - Seed completado")
print("  Email: inspector@demo.com")
print("  Password: demo1234")
