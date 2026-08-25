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
    # Optional one-time configure payload delivered to the device on its next
    # GET /v1/provision pull (e.g. {"tailscale_auth_key": "tskey-auth-..."}).
    # Stored until fetched once, then cleared server-side. See docs/PROVISIONING-V2.md.
    provision: Optional[dict[str, Any]] = None


class IssueCommandIn(BaseModel):
    type: str                       # restart | reset-clock | profile | set-peer
    args: dict[str, Any] = {}
    # Explicit intent for a command that interrupts service or changes what code runs. Defaults
    # to False so an existing caller cannot accidentally acquire the right to reboot a bridge
    # by omission - the safe direction for a field whose absence used to mean "go ahead".
    confirm: bool = False
    # Optional caller-supplied key for retry-safety. Two POSTs carrying the same key for the
    # same device return the SAME command instead of queuing a second one. Optional because
    # the in-flight guard below already protects the dangerous cases without any client
    # change; this is for callers that want the guarantee explicitly.
    idempotency_key: str | None = None


class RolloutCreateIn(BaseModel):
    version: str                       # target image version, e.g. "1.4.2"
    source: str                        # base URL/dir that holds manifest.txt + image
    stage_pct: Optional[int] = 10      # opening wave (10 -> 25 -> 50 -> 100)
