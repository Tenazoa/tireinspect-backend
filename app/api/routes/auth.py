from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from ...core.database import get_db
from ...core.security import verify_password, create_access_token
from ...models.models import Inspector
from ...api.deps import get_current_inspector

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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


@router.get("/me", response_model=InspectorOut)
def me(inspector: Inspector = Depends(get_current_inspector)):
    return to_out(inspector)
