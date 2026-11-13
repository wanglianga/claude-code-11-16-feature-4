from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, require_roles
from .. import models, schemas
from ..services.reconciliation import run_reconciliation

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=schemas.JobOut)
def create_job(
    body: schemas.JobIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles(models.ROLE_FINANCE)),
):
    ensure_site_access(db, user, body.site_id)
    site = db.get(models.Site, body.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if body.cycle_end <= body.cycle_start:
        raise HTTPException(400, "周期结束必须晚于开始")
    if body.rerun_of_job_id is not None:
        src = db.get(models.ReconciliationJob, body.rerun_of_job_id)
        if src is None or src.site_id != site.id:
            raise HTTPException(400, "重跑源作业不存在或站点不匹配")
    try:
        job = run_reconciliation(
            db, site, body.cycle_start, body.cycle_end,
            user.username, body.rerun_of_job_id,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f"核算失败: {exc}")
    db.refresh(job)
    return job


@router.get("", response_model=list[schemas.JobOut])
def list_jobs(site_id: int | None = None, db: Session = Depends(get_db),
              user: models.User = Depends(require_roles(
                  models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    stmt = select(models.ReconciliationJob).order_by(models.ReconciliationJob.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.ReconciliationJob.site_id == site_id)
    jobs = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        jobs = [j for j in jobs if j.site_id in allowed]
    return jobs


@router.get("/{job_id}", response_model=schemas.JobOut)
def get_job(job_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(require_roles(
                models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    job = db.get(models.ReconciliationJob, job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    ensure_site_access(db, user, job.site_id)
    return job


@router.get("/{job_id}/intervals", response_model=list[schemas.IntervalOut])
def job_intervals(job_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(
                      models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    job = db.get(models.ReconciliationJob, job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    ensure_site_access(db, user, job.site_id)
    return db.scalars(
        select(models.ReconciliationInterval)
        .where(models.ReconciliationInterval.job_id == job_id)
        .order_by(models.ReconciliationInterval.start_ts)
    ).all()


@router.get("/{job_id}/splits", response_model=list[schemas.SplitOut])
def job_splits(job_id: int, order_id: int | None = None,
               db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(
                   models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    job = db.get(models.ReconciliationJob, job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    ensure_site_access(db, user, job.site_id)
    stmt = select(models.OrderIntervalSplit).where(
        models.OrderIntervalSplit.job_id == job_id
    ).order_by(models.OrderIntervalSplit.start_ts)
    if order_id is not None:
        stmt = stmt.where(models.OrderIntervalSplit.order_id == order_id)
    return db.scalars(stmt).all()
