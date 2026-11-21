"""HaloFuse brand constants and the catalogue of user-facing messages.

Every string an artist can see for an error comes from MESSAGES, keyed by a stable code. Messages never contain a
URL, a host, an exception text or the name of the underlying cloud provider.
"""
from __future__ import annotations

import secrets

PRODUCT = "HaloFuse"
OUTPUT_DIRNAME = "HaloFuse"

_NOT_CONFIGURED = "HaloFuse is not configured on this computer. Contact your administrator."

MESSAGES: dict[str, str] = {
    "ok": "Connected to HaloFuse.",
    "not_configured": _NOT_CONFIGURED,
    "service_not_configured": _NOT_CONFIGURED,
    "credentials_missing": "No HaloFuse account is saved. Enter your account email and password in the Account section.",
    "connector_missing": "HaloFuse connector not installed. Ask your administrator to install the HaloFuse requirements.",
    "auth_failed": "HaloFuse could not sign in: the email or password was not accepted.",
    "forbidden": "HaloFuse signed in, but this account is not allowed to do that.",
    "rate_limited": "HaloFuse is receiving too many requests. Wait a minute and try again.",
    "service_unavailable": "The HaloFuse cloud service is temporarily unavailable. Try again in a few minutes.",
    "unreachable": "HaloFuse could not reach the cloud service. Check your internet connection and try again.",
    "no_capacity": "No HaloFuse GPU of this type is free right now. Try again later or pick another GPU.",
    "session_failed": "The HaloFuse GPU session could not be started.",
    "not_found": "HaloFuse could not find that item.",
    "storage_error": "HaloFuse cloud storage reported an error. Try again; if it repeats, check the HaloFuse log.",
    "too_large": "The file is larger than the HaloFuse upload limit.",
    "input_upload_failed": "HaloFuse could not upload the job inputs, so the job was cancelled.",
    "invalid_request": "HaloFuse rejected the request because a value was not valid.",
    "unknown": "HaloFuse hit an unexpected error. See the HaloFuse log for details.",
    "credentials_incomplete": "Enter both the HaloFuse account email and the password.",
    "credentials_managed_by_environment": (
        "HaloFuse credentials on this computer are managed by the deployment configuration and cannot be changed here."
    ),
    "no_secure_store": (
        "This computer has no system keychain. Use deployment configuration, or allow storing the password "
        "unencrypted on this computer."
    ),
    "credential_too_long": "The HaloFuse account email or password is too long.",
    "invalid_input": "HaloFuse could not read the request.",
    "forbidden_origin": "This HaloFuse action is only allowed from the ComfyUI page on this computer.",
    "missing_request_header": "This HaloFuse action must come from the HaloFuse panel.",
}

HTTP_STATUS: dict[str, int] = {
    "ok": 200,
    "not_configured": 503,
    "service_not_configured": 503,
    "credentials_missing": 409,
    "connector_missing": 503,
    "auth_failed": 409,
    "forbidden": 403,
    "rate_limited": 429,
    "service_unavailable": 503,
    "unreachable": 502,
    "no_capacity": 409,
    "session_failed": 409,
    "not_found": 404,
    "storage_error": 502,
    "too_large": 413,
    "input_upload_failed": 502,
    "invalid_request": 400,
    "unknown": 500,
    "credentials_incomplete": 400,
    "credentials_managed_by_environment": 409,
    "no_secure_store": 409,
    "credential_too_long": 400,
    "invalid_input": 400,
    "forbidden_origin": 403,
    "missing_request_header": 403,
}


def message(code: str) -> str:
    """Branded text for *code*; unknown codes get the generic message."""
    return MESSAGES.get(code) or MESSAGES["unknown"]


def status_for(code: str) -> int:
    return HTTP_STATUS.get(code, 500)


def new_error_id() -> str:
    """C-4 section 1 correlation id, e.g. "e-7f3a9c" (one format for every route; lead R-WS3-02)."""
    return "e-" + secrets.token_hex(3)


def error_envelope(code: str, *, error_id: str | None = None, message: str | None = None) -> dict:
    """The C-4 error envelope. *message* overrides the catalogue text (it must already be branded)."""
    text = message or MESSAGES.get(code) or MESSAGES["unknown"]
    return {"error": {"code": code, "message": text, "id": error_id or new_error_id()}}
