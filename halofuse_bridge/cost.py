"""Cost visibility and limits (B5, contract C-2 §3 `cost`).

Everything here is an *estimate*: the provider rate (from `estimate()`) times billable seconds taken from the
provider's own job timestamps. The provider SDK exposes no billed amount per job, so `actual_usd` stays null and no
price is ever invented: without a rate the amount is null.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .job_store import HaloFuseError

BASIS = "estimate"


def parse_ts(value) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string (``Z`` or offset). None when unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def rate_per_hour(value) -> float | None:
    """The provider's per-hour rate (a decimal string such as "2.10") as a float; None when absent or invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not rate.is_finite() or rate < 0:
        return None
    return float(rate)


def billable_seconds(started_running_at, terminal_at, now: float) -> float | None:
    """Seconds from the provider's start-of-running to its terminal time (or now while running)."""
    start = parse_ts(started_running_at)
    if start is None:
        return None
    end = parse_ts(terminal_at)
    end = now if end is None else end
    return max(0.0, end - start)


def estimate_usd(rate_per_hour_usd, seconds) -> float | None:
    rate = rate_per_hour(rate_per_hour_usd) if not isinstance(rate_per_hour_usd, float) else rate_per_hour_usd
    if rate is None or seconds is None:
        return None
    return round(rate * float(seconds) / 3600.0, 4)


def exceeds(amount, cap) -> bool:
    return amount is not None and cap is not None and float(amount) >= float(cap)


def setting_number(settings: dict | None, key: str, default=None):
    value = (settings or {}).get(key, default)
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_day(ts: float) -> tuple[int, int, int]:
    d = datetime.fromtimestamp(ts).date()
    return d.year, d.month, d.day


def daily_spend_usd(store, now: float) -> float:
    """Sum of per-job amounts (estimate, or actual when known) for records created on the local day of ``now``.

    Queue records carry no amount of their own (their chunks do), so nothing is counted twice.
    """
    day = _local_day(now)
    total = 0.0
    for s in store.list(limit=None):
        created = s.get("created_at")
        if created is None or s.get("kind") == "queue":
            continue
        if _local_day(float(created)) != day:
            continue
        if s.get("cost_usd") is not None:
            total += float(s["cost_usd"])
    return round(total, 4)


def check_daily_cap(store, settings: dict | None, now: float) -> None:
    cap = setting_number(settings, "daily_spend_cap_usd")
    if cap is None:
        return
    spent = daily_spend_usd(store, now)
    if spent >= cap:
        raise HaloFuseError(
            "daily_cap_reached",
            f"HaloFuse daily spending limit reached (estimate ${spent:.2f} of ${cap:.2f}). "
            "Raise the limit in settings or try again tomorrow.",
            409,
        )


def cost_summary(store, settings: dict | None, now: float) -> dict:
    active = []
    for s in store.list(active_only=True, limit=None):
        if s.get("kind") == "queue":
            continue
        active.append({"job_id": s["job_id"], "amount_usd": s.get("cost_usd")})
    return {
        "basis": BASIS,
        "today_usd": daily_spend_usd(store, now),
        "daily_cap_usd": setting_number(settings, "daily_spend_cap_usd"),
        "max_job_cost_usd": setting_number(settings, "max_job_cost_usd"),
        "active": active,
    }


def idle_warning_due(idle_since: float | None, now: float, warn_after_s: float, already_warned: bool) -> bool:
    """A warm session held idle (billed) for at least ``warn_after_s`` seconds, warned once per idle period."""
    return (not already_warned) and idle_since is not None and (now - idle_since) >= warn_after_s
