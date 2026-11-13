from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (
    ensure_site_access,
    ensure_task_access,
    get_current_user,
    require_roles,
    user_site_ids,
)
from .. import models, schemas

router = APIRouter(prefix="/api/tasks", tags=["review-tasks"])


@router.get("", response_model=list[schemas.TaskOut])
def list_tasks(site_id: int | None = None, status_filter: str | None = None,
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    stmt = select(models.ReviewTask).order_by(models.ReviewTask.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.ReviewTask.site_id == site_id)
    if status_filter:
        stmt = stmt.where(models.ReviewTask.status == status_filter)
    tasks = db.scalars(stmt).all()
    # 物业只能看到分配给自己角色且在授权站点内的核对事项
    if user.role == models.ROLE_PROPERTY:
        allowed = user_site_ids(db, user)
        tasks = [
            t for t in tasks
            if t.assignee == models.ROLE_PROPERTY and t.site_id in allowed
        ]
    return tasks


@router.post("/{task_id}/respond", response_model=schemas.TaskOut)
def respond_task(task_id: int, body: schemas.TaskRespondIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    task = db.get(models.ReviewTask, task_id)
    if task is None:
        raise HTTPException(404, "核对事项不存在")
    ensure_task_access(db, user, task)
    # 运营/财务只能处理对应角色条目；这里按角色与 assignee 一致性校验
    if user.role in (models.ROLE_OPERATOR, models.ROLE_FINANCE) and task.assignee != user.role:
        raise HTTPException(403, f"该事项分配给 {task.assignee}，你无权处理")
    task.status = models.TASK_SUBMITTED
    task.response = body.response
    task.responded_by = user.username
    task.responded_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task


@router.post("/{task_id}/resolve", response_model=schemas.TaskOut)
def resolve_task(task_id: int, body: schemas.TaskRespondIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    task = db.get(models.ReviewTask, task_id)
    if task is None:
        raise HTTPException(404, "核对事项不存在")
    ensure_site_access(db, user, task.site_id)
    task.status = models.TASK_RESOLVED
    task.response = body.response
    task.responded_by = user.username
    task.responded_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task
