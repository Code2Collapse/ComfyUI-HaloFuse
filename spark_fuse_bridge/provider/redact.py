"""One redaction layer for everything an artist or a log can see.

`redact(text)` removes the cloud endpoint (and every host of the same DNS domain), storage hosts, URL query strings,
bearer tokens, the account email and password, and the runtime image reference. The live values are registered by
the trusted layer through `register_secret()` and held in memory only.

`install()` makes the provider SDK, httpx/httpcore and bridge log records pass through `redact` at creation time
(a log-record factory, which also covers child loggers) and again through a logging.Filter on the named loggers.
CORE ComfyUI re-serves its console at /internal/logs, so these records are browser-visible otherwise.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import urllib.parse

WITHHELD = "[line withheld: redaction error]"

_LOCK = threading.Lock()
_HOSTS: set[str] = set()       # exact hostnames / IPs
_SUFFIXES: set[str] = set()    # registrable suffixes (last two DNS labels)
_LITERALS: dict[str, str] = {}  # literal -> replacement (case-sensitive)
_LITERALS_CI: dict[str, str] = {}  # literal (lowercase) -> replacement (case-insensitive)
_compiled: tuple | None = None  # (literal_re, literal_ci_re, bare_host_re) rebuilt on registration

_URL_RE = re.compile(r"""https?://[^\s'"<>]+""", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_PARAM_RE = re.compile(r"""(?i)\b(token|access_token|sig|signature|x-signature)=[^&\s'"]+""")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")

_PREFIXES = ("spark_fuse", "httpx", "httpcore", "hpack")
_FILTERED_LOGGERS = ("spark_fuse", "spark_fuse_bridge", "httpx", "httpcore")
_installed = False


def _hostname_of(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if "://" not in value:
        value = "//" + value
    try:
        host = urllib.parse.urlsplit(value).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def register_secret(value, kind: str) -> None:
    """Register a live secret. kind: endpoint | storage | email | password | token | image."""
    global _compiled
    if not value or not isinstance(value, str):
        return
    with _LOCK:
        if kind in ("endpoint", "storage"):
            host = _hostname_of(value)
            if not host:
                return
            _HOSTS.add(host)
            if not _is_ip(host):
                labels = host.split(".")
                _SUFFIXES.add(".".join(labels[-2:]) if len(labels) >= 2 else host)
        elif kind == "email":
            _LITERALS_CI[value.lower()] = "<account>"
        elif kind == "password":
            if len(value) < 4:
                return
            for form in {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
                         json.dumps(value)[1:-1]}:
                if len(form) >= 4:
                    _LITERALS[form] = "<secret>"
        elif kind == "token":
            if len(value) >= 4:
                _LITERALS[value] = "<token>"
        elif kind == "image":
            _LITERALS[value] = "<runtime-image>"
        else:
            return
        _compiled = None


def _patterns() -> tuple:
    global _compiled
    with _LOCK:
        if _compiled is None:
            lit = sorted(_LITERALS, key=len, reverse=True)
            lit_ci = sorted(_LITERALS_CI, key=len, reverse=True)
            names = sorted(_SUFFIXES | {h for h in _HOSTS if not _is_ip(h)}, key=len, reverse=True)
            ips = sorted(h for h in _HOSTS if _is_ip(h))
            _compiled = (
                re.compile("|".join(re.escape(x) for x in lit)) if lit else None,
                dict(_LITERALS),
                re.compile("|".join(re.escape(x) for x in lit_ci), re.IGNORECASE) if lit_ci else None,
                dict(_LITERALS_CI),
                re.compile(
                    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)*(?:" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9-])",
                    re.IGNORECASE) if names else None,
                re.compile("|".join(re.escape(i) for i in ips)) if ips else None,
                frozenset(_HOSTS), frozenset(_SUFFIXES),
            )
        return _compiled


def _host_is_service(host: str, hosts: frozenset, suffixes: frozenset) -> bool:
    host = host.lower()
    if host in hosts:
        return True
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _redact_url(match: re.Match, hosts: frozenset, suffixes: frozenset) -> str:
    url = match.group(0)
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "<url>"
    if host and _host_is_service(host, hosts, suffixes):
        return "<service-url>"
    if parts.query:
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + "?<redacted>"
    return url


def _redact_impl(text: str) -> str:
    lit_re, lit_map, ci_re, ci_map, host_re, ip_re, hosts, suffixes = _patterns()
    text = _URL_RE.sub(lambda m: _redact_url(m, hosts, suffixes), text)
    text = _BEARER_RE.sub("Bearer <token>", text)
    text = _TOKEN_PARAM_RE.sub(lambda m: m.group(1) + "=<token>", text)
    text = _JWT_RE.sub("<token>", text)
    if lit_re is not None:
        text = lit_re.sub(lambda m: lit_map[m.group(0)], text)
    if ci_re is not None:
        text = ci_re.sub(lambda m: ci_map[m.group(0).lower()], text)
    if host_re is not None:
        text = host_re.sub("<service>", text)
    if ip_re is not None:
        text = ip_re.sub("<service>", text)
    return text


def redact(text) -> str:
    """Return *text* with every registered secret and provider identifier removed. Fails closed."""
    try:
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        return _redact_impl(text)
    except Exception:  # noqa: BLE001 - fail closed, never leak the original
        return WITHHELD


def sanitize_line(text) -> str:
    """For relayed container/runner lines: no ANSI, no control chars, one line, redacted."""
    try:
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        text = _ANSI_RE.sub("", text)
        text = _NEWLINE_RE.sub(" ", text)
        text = _CTRL_RE.sub("", text)
    except Exception:  # noqa: BLE001
        return WITHHELD
    return redact(text)


# ---- logging integration -----------------------------------------------------------------------

def _scrub_record(record: logging.LogRecord) -> None:
    try:
        msg = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken %-format must not leak its args either
        msg = str(record.msg)
    record.msg = redact(msg)
    record.args = None
    if record.exc_info:
        try:
            text = logging.Formatter().formatException(record.exc_info)
        except Exception:  # noqa: BLE001
            text = WITHHELD
        record.exc_text = redact(text)
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def _watched(name: str) -> bool:
    return bool(name) and name.startswith(_PREFIXES)


class RedactingFilter(logging.Filter):
    """Logger filter (belt and braces for records made by a later-installed record factory)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "_halofuse_redacted", False):
            _scrub_record(record)
            record._halofuse_redacted = True
        return True


def install() -> None:
    """Idempotently route provider/httpx/bridge log records through `redact`."""
    global _installed
    with _LOCK:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            if _watched(record.name):
                _scrub_record(record)
                record._halofuse_redacted = True
            return record

        factory._halofuse = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
        for name in _FILTERED_LOGGERS:
            logger = logging.getLogger(name)
            if not any(isinstance(f, RedactingFilter) for f in logger.filters):
                logger.addFilter(RedactingFilter())
        _installed = True
