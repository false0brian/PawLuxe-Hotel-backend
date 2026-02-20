from dataclasses import dataclass
import hashlib
from typing import Literal

from fastapi import Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from app.core.config import settings
from app.db.models import AuthSession, User, utcnow
from app.db.session import get_session


Role = Literal["owner", "staff", "admin", "system"]


@dataclass(frozen=True)
class AuthContext:
    role: Role
    user_id: str


def _verify_api_key(x_api_key: str) -> None:
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def verify_api_key(x_api_key: str = Header(default=""), api_key: str = Query(default="")) -> None:
    _verify_api_key(x_api_key or api_key)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_auth_context(
    x_api_key: str = Header(default=""),
    x_session_token: str = Header(default=""),
    x_role: str = Header(default="owner"),
    x_user_id: str = Header(default=""),
    session: Session = Depends(get_session),
) -> AuthContext:
    _verify_api_key(x_api_key)
    token = x_session_token.strip()
    if token:
        token_hash = hash_session_token(token)
        row = session.exec(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .where(AuthSession.exp >= utcnow())
            .order_by(AuthSession.created_at.desc())
            .limit(1)
        ).first()
        if not row:
            raise HTTPException(status_code=403, detail="Invalid or expired x-session-token")
        user = session.get(User, row.user_id)
        if not user or not user.active:
            raise HTTPException(status_code=403, detail="Session user is inactive or missing")
        row.last_seen_at = utcnow()
        session.add(row)
        session.commit()
        return AuthContext(role=user.role, user_id=user.user_id)

    role = x_role.strip().lower()
    if role not in {"owner", "staff", "admin", "system"}:
        raise HTTPException(status_code=403, detail="x-role must be one of: owner, staff, admin, system")
    if role != "system" and not x_user_id.strip():
        raise HTTPException(status_code=403, detail="x-user-id is required for owner/staff/admin roles")
    return AuthContext(role=role, user_id=x_user_id.strip())


def require_owner_or_admin(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if auth.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Owner or admin role required")
    return auth


def require_staff_or_admin(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if auth.role not in {"staff", "admin"}:
        raise HTTPException(status_code=403, detail="Staff or admin role required")
    return auth


def require_admin_or_system(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if auth.role not in {"admin", "system"}:
        raise HTTPException(status_code=403, detail="Admin or system role required")
    return auth
