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
    # done | failed | rejected. Deliberately a plain str: command_result() records anything else as
    # "failed" (keeping the raw word in the output) rather than answering 422, because the agent
    # keeps retrying a result the fleet refuses and the command would never reach a final state.
    status: str
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
    # `update` to an OS version OLDER than the bridge runs is refused unless this is set. A 2.1
    # image on a 2.2 bridge drops it back to PIN protocol 1, which the presenter app refuses and
    # which the fleet can no longer update remotely - a physical reflash (2026-09-28).
    allow_downgrade: bool = False


class DeviceUpdateIn(BaseModel):
    """Admin edits to a bridge's identity in the fleet (PATCH /admin/devices/{id})."""
    name: Optional[str] = None         # display name, 1-64 characters
    number: Optional[int] = None       # fleet number, shown as NB-001; unique within the org


class RolloutCreateIn(BaseModel):
    version: str                       # target image version, e.g. "1.4.2"
    source: str                        # base URL/dir that holds manifest.txt + image
    stage_pct: Optional[int] = 10      # opening wave (10 -> 25 -> 50 -> 100)
    # Include bridges that run a NEWER version than this one (a deliberate fleet downgrade).
    # Without it they are left out and named, for the reason given on IssueCommandIn.
    allow_downgrade: bool = False
