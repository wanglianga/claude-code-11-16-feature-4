from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, get_current_user, require_roles
from .. import models, schemas
from ..services.importer import import_file, ImportError_

router = APIRouter(prefix="/api/imports", tags=["imports"])

STORAGE_DIR = "data/originals"


@router.post("", response_model=schemas.ImportResultOut)
def upload_import(
    site_id: int = Form(...),
    source_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles(models.ROLE_FINANCE, models.ROLE_OPERATOR)),
):
    ensure_site_access(db, user, site_id)
    site = db.get(models.Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    content = file.file.read()
    try:
        result = import_file(
            db, site, source_type, file.filename or "upload.csv",
            content, user.username, STORAGE_DIR,
        )
        db.commit()
    except ImportError_ as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"导入失败: {exc}")
    return result


@router.get("/files", response_model=list[schemas.ImportFileOut])
def list_files(site_id: int | None = None, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    stmt = select(models.ImportFile).order_by(models.ImportFile.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.ImportFile.site_id == site_id)
    files = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        files = [f for f in files if f.site_id in allowed]
    return files


@router.get("/files/{file_id}/rows", response_model=list[schemas.ImportRowOut])
def file_rows(file_id: int, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    imp = db.get(models.ImportFile, file_id)
    if imp is None:
        raise HTTPException(404, "文件不存在")
    ensure_site_access(db, user, imp.site_id)
    return db.scalars(
        select(models.ImportRow)
        .where(models.ImportRow.import_file_id == file_id)
        .order_by(models.ImportRow.line_no)
    ).all()
