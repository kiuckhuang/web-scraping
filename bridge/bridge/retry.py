"""Shared Retry-After parsing for the optional cloud transports."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    """Parse an RFC 9110 Retry-After value (delta-seconds or HTTP-date).

    Returns the delay in seconds, or None when the value is absent, negative
    or unparseable — an upstream quirk must never turn a documented 429 into
    an unhandled ValueError (and therewith an opaque 502).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    delta = (target - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, delta)
