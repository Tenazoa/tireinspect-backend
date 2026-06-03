import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .core.config import settings
from .core.database import Base, engine
from .core.seed import seed_if_empty
from .api.routes import auth, vehicles, inspections, photos, ai, fleet

# Crear tablas al iniciar y sembrar datos demo si está vacía (útil en la nube)
Base.metadata.create_all(bind=engine)
seed_if_empty()
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


@app.get("/health")
def health():
    return {"status": "ok"}
