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
import time
import re
import urllib.parse
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


_TOKEN_CACHE: dict = {"token": "", "exp": 0.0}


def _access_token() -> str:
    """Return a bearer token usable against the Tailscale API.

    A plain API key (tskey-api-...) IS a bearer token and is used as-is. An OAuth client
    SECRET (tskey-client-...) is not: it must first be exchanged for a short-lived access
    token via the client-credentials flow. Sending the secret directly authenticates far
    enough for Tailscale to identify an actor and then refuse with

        403 "calling actor does not have enough permissions to perform this function"

    which reads like a scope problem and sends you back to the console to re-tick boxes
    that were already correct. It is not a scope problem - it is the wrong kind of token.

    The access token is cached until shortly before it expires, so a fleet claiming many
    devices does not perform an exchange per device.
    """
    raw = (settings.ts_api_key or "").strip()
    if not raw:
        raise MeshNotConfigured("TS_API_KEY not set")
    if not raw.startswith("tskey-client-"):
        return raw                      # plain API key

    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now + 30:
        return _TOKEN_CACHE["token"]

    # tskey-client-<CLIENT_ID>-<secret>; Tailscale wants the id alongside the secret.
    bits = raw.split("-")
    client_id = bits[2] if len(bits) > 3 else ""
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": raw,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        "https://api.tailscale.com/api/v2/oauth/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise MeshError("tailscale oauth token %s: %s" % (e.code, detail))
    except Exception as e:
        raise MeshError("tailscale oauth token: %s" % e)

    tok = out.get("access_token") or ""
    if not tok:
        raise MeshError("tailscale oauth token: no access_token in response")
    _TOKEN_CACHE["token"] = tok
    _TOKEN_CACHE["exp"] = now + float(out.get("expires_in") or 3600)
    return tok


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
                 "Authorization": "Bearer " + _access_token()})
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
