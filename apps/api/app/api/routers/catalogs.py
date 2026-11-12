from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import func, or_, select

from app.api.deps import DB, PrincipalDep, SettingsDep, clean_param
from app.core.errors import NotFoundError, PayloadTooLarge, ValidationFailed
from app.models.catalog import Catalog, CatalogVersion, Product
from app.schemas.common import CatalogOut, CatalogVersionOut, Page, ProductOut, ProductPrivileged
from app.services import catalog_import
from app.services.authz import Permission, has_permission
from app.services.ratelimit import limit_catalog_import

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLarge(f"File exceeds the maximum size of {max_bytes // (1024 * 1024)} MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/active-version")
def active_version(principal: PrincipalDep, db: DB) -> dict[str, str | None]:
    """The catalog version new ad-hoc quote lines price from (the most recently activated catalog wins)."""
    from app.services.catalog_import import active_version_for_org

    v = active_version_for_org(db, principal.org_id)
    return {"version_id": str(v.id) if v else None}


@router.get("", response_model=list[CatalogOut])
def list_catalogs(principal: PrincipalDep, db: DB) -> list[CatalogOut]:
    rows = db.scalars(
        select(Catalog).where(Catalog.organization_id == principal.org_id).order_by(Catalog.created_at)
    ).all()
    return [CatalogOut.model_validate(c) for c in rows]


@router.post("/import", response_model=CatalogVersionOut, status_code=201)
async def import_catalog(
    principal: PrincipalDep,
    db: DB,
    settings: SettingsDep,
    file: UploadFile = File(...),
    mapping: str = Form(...),
    catalog_name: str | None = Form(default=None),
) -> CatalogVersionOut:
    principal.require(Permission.IMPORT_CATALOG)
    limit_catalog_import(principal.user.id)
    try:
        mapping_obj = json.loads(mapping)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("mapping must be a JSON object") from exc
    if not isinstance(mapping_obj, dict):
        raise ValidationFailed("mapping must be a JSON object")
    content = await read_upload(file, settings.max_upload_bytes)
    version = catalog_import.import_catalog(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        filename=file.filename or "catalog",
        content=content,
        mapping=mapping_obj,
        catalog_name=catalog_name,
        default_currency=principal.organization.default_currency,
    )
    return CatalogVersionOut.model_validate(version)


@router.post("/versions/{version_id}/activate", response_model=CatalogVersionOut)
def activate(version_id: uuid.UUID, principal: PrincipalDep, db: DB) -> CatalogVersionOut:
    principal.require(Permission.IMPORT_CATALOG)
    version = catalog_import.activate_version(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        ip=principal.ip,
        version_id=version_id,
    )
    return CatalogVersionOut.model_validate(version)


@router.get("/versions/{version_id}/products", response_model=Page)
def products(
    version_id: uuid.UUID,
    principal: PrincipalDep,
    db: DB,
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page:
    version = db.scalar(
        select(CatalogVersion).where(
            CatalogVersion.id == version_id, CatalogVersion.organization_id == principal.org_id
        )
    )
    if version is None:
        raise NotFoundError("Catalog version not found")
    stmt = select(Product).where(Product.catalog_version_id == version.id, Product.organization_id == principal.org_id)
    q = clean_param(q)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(or_(Product.sku.ilike(like, escape="\\"), Product.description.ilike(like, escape="\\")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Product.sku).offset((page - 1) * page_size).limit(page_size)).all()
    model = ProductPrivileged if has_permission(principal.role, Permission.VIEW_COSTS) else ProductOut
    return Page(
        items=[model.model_validate(p).model_dump(mode="json") for p in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
