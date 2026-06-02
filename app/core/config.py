from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Default SQLite para arranque simple en la nube (sin configurar nada).
    # Si se define DATABASE_URL (ej. PostgreSQL de Render), se usa esa.
    DATABASE_URL: str = "sqlite:///./tireinspect.db"
    SECRET_KEY: str = "change-me-in-production-tireinspect-2026"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 días

    UPLOAD_DIR: str = "./uploads"
    S3_PUBLIC_URL: str = "http://localhost:8000/uploads"
    S3_BUCKET: str = "local"
    S3_ENDPOINT: str = "local"
    AWS_ACCESS_KEY_ID: str = "local"
    AWS_SECRET_ACCESS_KEY: str = "local"

    FRONTEND_URL: str = "http://localhost:3000"

    class Config:
        env_file = ".env"


settings = Settings()
