import uuid
import base64
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
from ...core.database import get_db
from ...api.deps import get_current_inspector
from ...models.models import Inspector, PhotoBlob

router = APIRouter(prefix="/photos", tags=["photos"])


class PhotoUploadOut(BaseModel):
    url: str


@router.post("/upload", response_model=PhotoUploadOut)
async def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    tire_inspection_id: str = Form(...),
    db: Session = Depends(get_db),
    _: Inspector = Depends(get_current_inspector),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")
    content = await file.read()
    blob = PhotoBlob(
        id=str(uuid.uuid4()),
        content_type=file.content_type or "image/jpeg",
        data=base64.b64encode(content).decode("ascii"),
    )
    db.add(blob)
    db.commit()
    base = str(request.base_url).rstrip("/")
    return PhotoUploadOut(url=f"{base}/api/v1/photos/img/{blob.id}")


@router.get("/img/{photo_id}")
def get_photo(photo_id: str, db: Session = Depends(get_db)):
    blob = db.get(PhotoBlob, photo_id)
    if not blob:
        raise HTTPException(404, "Foto no encontrada")
    return Response(content=base64.b64decode(blob.data), media_type=blob.content_type)
