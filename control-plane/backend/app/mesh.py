"""Mesh (Tailscale) auth-key minting for the presenter app — walkthrough J3 phase 5:
"Joins the private mesh with an embedded client + scoped token from sign-in", and
the "gone from today": installing Tailscale · knowing the 100.x IP · the shared admin key.

The control plane NEVER hands the app an admin or reusable key. On each sign-in it
mints, via the Tailscale API, a key that is:
  * ephemeral      — the source node auto-removes when the app disconnects, so the
                     tailnet never fills up with stale "nb-source" nodes.
  * preauthorized  — usable immediately, no admin has to approve the node.
  * tagged         — tag:nb-source. THIS is the scope: the tailnet ACL grants
                     tag:nb-source reachability to the bridges only, nothing else.
                     A leaked key can reach bridges, never other nodes, and it
                     dies on disconnect + expiry.
  * short-lived    — the KEY itself is only valid to join for a few minutes.

Dependency-free (urllib) to keep the backend stdlib-only. If the Tailscale API is
not configured (settings.ts_api_key empty), minting raises MeshNotConfigured and
the endpoint answers 503 — the feature is simply off, not broken.
"""
import json
import re
import urllib.request
import urllib.error

from .config import settings


def _safe_desc(s: str) -> str:
    """Tailscale rejects key descriptions containing characters like @ ( ) .
    (seen live: 'description had invalid characters'). Keep a conservative subset —
    letters, numbers, spaces, hyphen, underscore — so an email/org label like
    'samithreddy72@gmail.com (default)' becomes a valid 'samithreddy72 at gmail com
    default'. Still audit-useful, always accepted."""
    s = s.replace("@", " at ")
    s = re.sub(r"[^A-Za-z0-9 _-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200] or "netbridge-source"


class MeshNotConfigured(Exception):
    """settings.ts_api_key is unset — the mesh feature is not enabled here."""


class MeshError(Exception):
    """The Tailscale API rejected the request or was unreachable."""


def _tailnet() -> str:
    # "-" tells the Tailscale API to use the default tailnet of the calling key.
    return settings.ts_tailnet or "-"


def _source_tags() -> list[str]:
    return [t.strip() for t in settings.ts_source_tag.split(",") if t.strip()]


def mint_ephemeral_key(description: str, tags: list[str] | None = None) -> dict:
    """Create one scoped ephemeral auth key. Returns {"key": "tskey-auth-…",
    "expires": <iso8601|None>}. Raises MeshNotConfigured if unconfigured, MeshError
    on API failure. Separated from the endpoint so it can be mocked in tests."""
    if not settings.ts_api_key:
        raise MeshNotConfigured("TS_API_KEY not set")
    url = "https://api.tailscale.com/api/v2/tailnet/%s/keys" % _tailnet()
    body = {
        "capabilities": {"devices": {"create": {
            "reusable": False,
            "ephemeral": True,
            "preauthorized": True,
            "tags": tags or _source_tags(),
        }}},
        "expirySeconds": settings.ts_key_ttl_s,
        "description": _safe_desc(description),
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 # Tailscale accepts an API key or OAuth access token as bearer.
                 "Authorization": "Bearer " + settings.ts_api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise MeshError("tailscale API %s: %s" % (e.code, detail))
    except Exception as e:
        raise MeshError("tailscale API unreachable: %s" % e)
    key = out.get("key")
    if not key:
        raise MeshError("tailscale API returned no key")
    return {"key": key, "expires": out.get("expires")}
