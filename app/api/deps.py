from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from ..core.database import get_db
from ..core.security import decode_token
from ..models.models import Inspector

bearer_scheme = HTTPBearer()


def get_current_inspector(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Inspector:
    inspector_id = decode_token(credentials.credentials)
    inspector = db.get(Inspector, inspector_id)
    if not inspector or not inspector.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inspector no encontrado")
    return inspector
