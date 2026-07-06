"""Pydantic request/response schemas."""
from typing import Any, Optional
from pydantic import BaseModel


class EnrollIn(BaseModel):
    bootstrap_token: str
    device_id: str
    pairing_code: str
    version: Optional[str] = None
    tailscale_ip: Optional[str] = None
    hostname: Optional[str] = None


class EnrollOut(BaseModel):
    device_id: str
    device_token: str


class CommandOut(BaseModel):
    id: int
    type: str
    args: dict[str, Any] = {}


class CommandResultIn(BaseModel):
    status: str            # done | failed | rejected
    output: Optional[str] = None


class ClaimIn(BaseModel):
    name: str


class IssueCommandIn(BaseModel):
    type: str                       # restart | reset-clock | profile | set-peer
    args: dict[str, Any] = {}
