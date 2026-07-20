"""Auth helpers: device-token auth for the agent, API-key auth for the admin panel."""
import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Device


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def new_device_token() -> tuple[str, str]:
    tok = secrets.token_urlsafe(32)
    return tok, hash_token(tok)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def require_device(authorization: str | None = Header(default=None),
                   db: Session = Depends(get_db)) -> Device:
    tok = _bearer(authorization)
    dev = db.scalar(select(Device).where(Device.token_hash == hash_token(tok)))
    if not dev:
        raise HTTPException(401, "invalid device token")
    return dev


def _user_for_token(tok: str, db: Session):
    from .models import User, utcnow
    u = db.scalar(select(User).where(User.token_hash == hash_token(tok)))
    if u:
        u.last_seen = utcnow()
        db.commit()
    return u


def _legacy_key_ok(tok: str, db: Session) -> bool:
    """The shared org-wide admin key (walkthrough J4 lists `dev-admin-key` under
    "gone from today"). It is now a BOOTSTRAP credential only: it works while the
    fleet has no admin user yet, so a fresh deployment can create its first one,
    and retires itself automatically the moment that account exists.

    Retiring it matters because it is one secret shared by every operator: it
    cannot be revoked for one person, it cannot be attributed in the audit log
    (every action it takes is logged as "legacy-key", not a name), and it never
    expires.
    """
    from .models import User
    if not settings.admin_api_key:
        return False           # unset = disabled. Without this an empty key
                               # would make `Authorization: Bearer ` a bypass.
    if not secrets.compare_digest(tok, settings.admin_api_key):
        return False
    # Any real admin account existing means bootstrap is over.
    return db.scalar(select(User).where(User.role == "admin")) is None


def require_admin(authorization: str | None = Header(default=None),
                  db: Session = Depends(get_db)) -> str:
    """Full fleet control: a personal token whose user has role=admin (or the
    bootstrap key while no admin exists). Returns the actor's identity for the
    audit log."""
    tok = _bearer(authorization)
    u = _user_for_token(tok, db)
    if u and u.role == "admin":
        return u.email
    if _legacy_key_ok(tok, db):
        return "bootstrap-key"
    raise HTTPException(401, "invalid admin credential")


def require_viewer(authorization: str | None = Header(default=None),
                   db: Session = Depends(get_db)) -> str:
    """Read-only fleet visibility: any personal token (admin or presenter), or
    the bootstrap key while no admin exists. Presenters can see bridges — never
    command them."""
    tok = _bearer(authorization)
    u = _user_for_token(tok, db)
    if u:
        return u.email
    if _legacy_key_ok(tok, db):
        return "bootstrap-key"
    raise HTTPException(401, "invalid credential")
