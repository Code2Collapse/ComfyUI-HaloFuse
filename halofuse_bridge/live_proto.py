"""Live ComfyUI: short-lived signed tokens and the header rules shared by the bridge and the relay gateway (L9.345).

Stdlib only. A token is ``<base64url(json claims)>.<base64url(hmac-sha256)>``; claims = sid, role, user, exp, nonce.
Roles: "agent" (the cloud container dialling the gateway), "viewer" (the one-shot link the panel opens),
"cookie" (the browser session the gateway issues in exchange for a viewer token) and "bridge" (status polls).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ROLES = ("agent", "viewer", "cookie", "bridge")
COOKIE_NAME = "hf_live"
HOP_BY_HOP = frozenset(h.lower() for h in (
    "Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization", "TE", "Trailer", "Trailers",
    "Transfer-Encoding", "Upgrade", "Host", "Content-Length", "Content-Encoding",
    "Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Extensions", "Sec-WebSocket-Accept",
    "Sec-WebSocket-Protocol"))
# never forwarded to the cloud ComfyUI: they would carry the gateway's own credentials
CREDENTIAL_HEADERS = frozenset(("authorization", "cookie", "set-cookie"))


class TokenError(ValueError):
    """The token is missing, malformed, tampered, expired, for another role or another session."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret) -> bytes:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret or b"")
    if len(key) < 16:
        raise TokenError("the live gateway secret is missing or shorter than 16 characters")
    return key


def mint(secret, sid: str, role: str, user: str, ttl_s: float, now: float | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown token role {role!r}")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    now = time.time() if now is None else now
    claims = {"sid": str(sid), "role": role, "user": str(user), "exp": int(now + ttl_s),
              "nonce": secrets.token_urlsafe(8)}
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64(hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(secret, token, role: str, sid: str | None = None, now: float | None = None) -> dict:
    """The claims of a valid token, else TokenError. Constant-time signature check before anything is parsed."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4096:
        raise TokenError("malformed token")
    body, sig = token.split(".")
    want = _b64(hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        raise TokenError("bad signature")
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        raise TokenError("malformed claims") from None
    if not isinstance(claims, dict) or claims.get("role") != role:
        raise TokenError("wrong role")
    if sid is not None and claims.get("sid") != sid:
        raise TokenError("wrong session")
    now = time.time() if now is None else now
    if not isinstance(claims.get("exp"), int) or now >= claims["exp"]:
        raise TokenError("expired")
    return claims


def forward_headers(headers) -> dict:
    """Request/response headers safe to relay: hop-by-hop and credential headers removed."""
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in CREDENTIAL_HEADERS:
            continue
        out[k] = v
    return out


def bearer(headers) -> str | None:
    auth = headers.get("Authorization") or ""
    return auth[7:].strip() if auth[:7].lower() == "bearer " else None
