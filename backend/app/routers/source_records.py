from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, get_current_user
from .. import models, schemas

router = APIRouter(prefix="/api/source-records", tags=["source-records"])


@router.get("", response_model=list[schemas.SourceRecordOut])
def list_source_records(site_id: int, source_type: str | None = None,
                        db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    stmt = select(models.SourceRecord).where(models.SourceRecord.site_id == site_id)
    if source_type:
        stmt = stmt.where(models.SourceRecord.source_type == source_type)
    records = db.scalars(stmt.order_by(models.SourceRecord.id.desc())).all()
    return records


@router.get("/{record_id}/revisions", response_model=list[schemas.RevisionOut])
def list_revisions(record_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    sr = db.get(models.SourceRecord, record_id)
    if sr is None:
        raise HTTPException(404, "来源记录不存在")
    ensure_site_access(db, user, sr.site_id)
    return db.scalars(
        select(models.RecordRevision)
        .where(models.RecordRevision.source_record_id == record_id)
        .order_by(models.RecordRevision.revision_no)
    ).all()
