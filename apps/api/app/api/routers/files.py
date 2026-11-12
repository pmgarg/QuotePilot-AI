from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import DB, PrincipalDep
from app.core.errors import NotFoundError
from app.models.files import UploadedFile
from app.services import audit
from app.services.storage import get_storage, iter_file, safe_filename

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{file_id}")
def download(file_id: uuid.UUID, principal: PrincipalDep, db: DB) -> StreamingResponse:
    upload = db.scalar(
        select(UploadedFile).where(UploadedFile.id == file_id, UploadedFile.organization_id == principal.org_id)
    )
    if upload is None:
        raise NotFoundError("File not found")
    storage = get_storage()
    if not storage.exists(upload.storage_key):
        raise NotFoundError("File not found")
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="file.download",
        entity_type="uploaded_file",
        entity_id=upload.id,
        ip=principal.ip,
    )
    db.commit()
    name = safe_filename(upload.original_name)
    return StreamingResponse(
        iter_file(storage.open(upload.storage_key)),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
