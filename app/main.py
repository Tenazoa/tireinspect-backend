import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .core.config import settings
from .core.database import Base, engine
from .core.seed import seed_if_empty
from .api.routes import auth, vehicles, inspections, photos, ai, fleet

# Crear tablas al iniciar y sembrar datos demo si está vacía (útil en la nube).
# Tolerante a fallos: si la BD está pausada/caída, la app igual arranca y responde
# (así el servicio no queda en crash-loop y se recupera solo al volver la BD).
try:
    Base.metadata.create_all(bind=engine)
except Exception as _e:
    print(f"[startup] No se pudo crear/verificar tablas (BD no disponible): {_e}")


def _migrate():
    """Migraciones idempotentes para columnas nuevas en tablas existentes."""
    from sqlalchemy import text
    dialect = engine.dialect.name
    stmts = []
    if dialect == "postgresql":
        stmts.append("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN IF NOT EXISTS km_total DOUBLE PRECISION")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN IF NOT EXISTS km_life DOUBLE PRECISION")
    else:  # sqlite u otros: intentar y tolerar si ya existe
        stmts.append("ALTER TABLE vehicles ADD COLUMN active BOOLEAN DEFAULT 1")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN km_total FLOAT")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN km_life FLOAT")
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception:
                pass


try:
    _migrate()
except Exception as _e:
    print(f"[startup] Migración omitida (BD no disponible): {_e}")
try:
    seed_if_empty()
except Exception as _e:
    print(f"[startup] Seed omitido (BD no disponible): {_e}")


def _bootstrap_admin():
    """Asegura que el dueño tenga rol admin."""
    from .core.database import SessionLocal
    from .models.models import Inspector
    db = SessionLocal()
    try:
        u = db.query(Inspector).filter(Inspector.email == "tenazoapedro77@gmail.com").first()
        if u and (u.role != "admin" or not u.is_active):
            u.role = "admin"
            u.is_active = True
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


try:
    _bootstrap_admin()
except Exception as _e:
    print(f"[startup] Bootstrap admin omitido: {_e}")
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="TireInspect API",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev: permite celular en red local
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(vehicles.router, prefix="/api/v1")
app.include_router(inspections.router, prefix="/api/v1")
app.include_router(photos.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(fleet.router, prefix="/api/v1")

# Servir fotos subidas localmente
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    """Ping de salud: consulta la BD para mantener activo el proyecto (Supabase pausa por inactividad)."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "up"}
    except Exception as e:
        return {"status": "degraded", "db": "down", "detail": str(e)[:120]}
