import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .core.config import settings
from .core.database import Base, engine
from .core.seed import seed_if_empty
from .api.routes import auth, vehicles, inspections, photos, ai, fleet, gestion

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
        stmts.append("ALTER TABLE tire_stock ADD COLUMN IF NOT EXISTS km_life DOUBLE PRECISION")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN IF NOT EXISTS estimado_km DOUBLE PRECISION")
        stmts.append("ALTER TABLE tire_stock ADD COLUMN IF NOT EXISTS estimado_km DOUBLE PRECISION")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN IF NOT EXISTS tipo_unidad VARCHAR")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN IF NOT EXISTS tipo_vehiculo VARCHAR")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN IF NOT EXISTS marca VARCHAR")
        stmts.append("ALTER TABLE reencauche_tires ADD COLUMN IF NOT EXISTS km_recorrido DOUBLE PRECISION")
        # Seguridad: activar Row Level Security en TODAS las tablas públicas
        # (cierra el aviso rls_disabled_in_public de Supabase para tablas nuevas).
        # El backend se conecta como dueño/postgres, que ignora RLS, así que no
        # afecta su funcionamiento; solo bloquea el acceso anónimo por la API.
        stmts.append(
            "DO $$ DECLARE r record; BEGIN "
            "FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP "
            "EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', r.tablename); "
            "END LOOP; END $$;"
        )
    else:  # sqlite u otros: intentar y tolerar si ya existe
        stmts.append("ALTER TABLE vehicles ADD COLUMN active BOOLEAN DEFAULT 1")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN km_total FLOAT")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN km_life FLOAT")
        stmts.append("ALTER TABLE tire_stock ADD COLUMN km_life FLOAT")
        stmts.append("ALTER TABLE tire_specs ADD COLUMN estimado_km FLOAT")
        stmts.append("ALTER TABLE tire_stock ADD COLUMN estimado_km FLOAT")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN tipo_unidad VARCHAR")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN tipo_vehiculo VARCHAR")
        stmts.append("ALTER TABLE vehicle_vigilancia ADD COLUMN marca VARCHAR")
        stmts.append("ALTER TABLE reencauche_tires ADD COLUMN km_recorrido FLOAT")
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

# Aviso de seguridad: si en producción (Postgres) sigue la SECRET_KEY por
# defecto, cualquiera podría firmar un JWT válido. Debe definirse la variable de
# entorno SECRET_KEY en Render. No se rota automáticamente aquí a propósito:
# hacerlo cerraría la sesión de todos en cada despliegue.
if engine.dialect.name == "postgresql" and settings.SECRET_KEY == "change-me-in-production-tireinspect-2026":
    print("[SEGURIDAD] SECRET_KEY sigue con el valor por defecto en producción. "
          "Define la variable de entorno SECRET_KEY en Render (una cadena larga y aleatoria).")

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="TireInspect API",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # permite celular en red local y la web en Vercel
    # La autenticación va por header Bearer (no cookies), así que no se necesitan
    # credenciales CORS. Con allow_credentials=True el navegador RECHAZA la
    # respuesta cuando el origen es "*", rompiendo cualquier dashboard web de otro
    # origen; con False, "*" funciona correctamente.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Descargo-Monto", "X-Descargo-Causa", "X-Descargo-Id"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(vehicles.router, prefix="/api/v1")
app.include_router(inspections.router, prefix="/api/v1")
app.include_router(photos.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(fleet.router, prefix="/api/v1")
app.include_router(gestion.router, prefix="/api/v1")

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
