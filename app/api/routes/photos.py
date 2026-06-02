import uuid
import os
import aiofiles
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ...core.config import settings
from ...api.deps import get_current_inspector
from ...models.models import Inspector

router = APIRouter(prefix="/photos", tags=["photos"])

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)


class PhotoUploadOut(BaseModel):
    url: str


@router.post("/upload", response_model=PhotoUploadOut)
async def upload_photo(
    file: UploadFile = File(...),
    tire_inspection_id: str = Form(...),
    _: Inspector = Depends(get_current_inspector),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")

    ext = "jpg"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()

    subdir = os.path.join(settings.UPLOAD_DIR, "tires", tire_inspection_id)
    os.makedirs(subdir, exist_ok=True)

    filename = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join(subdir, filename)

    content = await file.read()
    async with aiofiles.open(filepath, "wb") as f:
        await f.write(content)

    relative = f"tires/{tire_inspection_id}/{filename}"
    url = f"{settings.S3_PUBLIC_URL}/{relative}"
    return PhotoUploadOut(url=url)
