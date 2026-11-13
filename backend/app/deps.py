from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    try:
        payload = decode_token(token)
        username = payload["sub"]
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证失败")
    user = db.scalar(select(models.User).where(models.User.username == username))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    return user


def require_roles(*roles: str):
    def checker(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"需要角色: {', '.join(roles)}")
        return user
    return checker


def user_site_ids(db: Session, user: models.User) -> set[int]:
    if user.role == models.ROLE_PROPERTY:
        rows = db.scalars(
            select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
        ).all()
        return set(rows)
    return set()  # 非物业角色返回空集合表示“不限制”


def ensure_site_access(db: Session, user: models.User, site_id: int) -> None:
    """物业只能访问授权站点；财务/运营可访问全部站点。"""
    if user.role == models.ROLE_PROPERTY and site_id not in user_site_ids(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该站点不在你的核对授权范围内")


def ensure_task_access(db: Session, user: models.User, task: models.ReviewTask) -> None:
    """物业只能处理 assignee=property 且在授权站点内的核对事项。"""
    if user.role == models.ROLE_PROPERTY:
        if task.assignee != models.ROLE_PROPERTY or task.site_id not in user_site_ids(db, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "该核对事项未分配给你处理")
