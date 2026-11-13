from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, get_current_user
from .. import models, schemas

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.get("", response_model=list[schemas.SiteOut])
def list_sites(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    sites = db.scalars(select(models.Site).order_by(models.Site.id)).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        sites = [s for s in sites if s.id in allowed]
    return sites


@router.get("/{site_id}", response_model=schemas.SiteOut)
def get_site(site_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    ensure_site_access(db, user, site_id)
    site = db.get(models.Site, site_id)
    if site is None:
        from fastapi import HTTPException
        raise HTTPException(404, "站点不存在")
    return site
