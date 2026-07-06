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


def require_admin(authorization: str | None = Header(default=None)) -> bool:
    tok = _bearer(authorization)
    # constant-time compare
    if not secrets.compare_digest(tok, settings.admin_api_key):
        raise HTTPException(401, "invalid admin key")
    return True
