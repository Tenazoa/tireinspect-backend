from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
import uuid
from ...core.database import get_db
from ...core.security import verify_password, create_access_token, hash_password
from ...models.models import Inspector, Company
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str


class InspectorOut(BaseModel):
    id: str
    name: str
    email: str
    company: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    inspector: InspectorOut


def to_out(inspector: Inspector) -> InspectorOut:
    return InspectorOut(
        id=inspector.id,
        name=inspector.name,
        email=inspector.email,
        company=inspector.company.name if inspector.company else "",
        role=inspector.role,
    )


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    inspector = db.query(Inspector).filter(Inspector.email == body.email).first()
    if not inspector or not verify_password(body.password, inspector.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales incorrectas")
    if not inspector.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta inactiva")
    return LoginResponse(access_token=create_access_token(inspector.id), inspector=to_out(inspector))


@router.post("/register", response_model=LoginResponse)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Crea una cuenta propia y la asocia a la empresa. Inicia sesión automáticamente."""
    email = body.email.strip().lower()
    if db.query(Inspector).filter(Inspector.email == email).first():
        raise HTTPException(status_code=400, detail="Ya existe una cuenta con ese correo")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")

    company = db.query(Company).first()
    if company is None:
        company = Company(id=str(uuid.uuid4()), name="TYMSAC")
        db.add(company)
        db.flush()
    elif company.name in ("Flota Demo SA", "Flota Demo", "Demo"):
        company.name = "TYMSAC"

    inspector = Inspector(
        id=str(uuid.uuid4()),
        name=body.name.strip() or email,
        email=email,
        hashed_password=hash_password(body.password),
        role="inspector",
        is_active=True,
        company_id=company.id,
    )
    db.add(inspector)
    db.commit()
    return LoginResponse(access_token=create_access_token(inspector.id), inspector=to_out(inspector))


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    inspector: Inspector = Depends(get_current_inspector),
):
    if not verify_password(body.currentPassword, inspector.hashed_password):
        raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta")
    if len(body.newPassword) < 6:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres")
    inspector.hashed_password = hash_password(body.newPassword)
    db.commit()
    return {"ok": True}


@router.get("/me", response_model=InspectorOut)
def me(inspector: Inspector = Depends(get_current_inspector)):
    return to_out(inspector)
