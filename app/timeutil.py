"""Timestamp helpers. The API and database use integer epoch milliseconds UTC."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

UTC = timezone.utc


def parse_timestamp(value: str) -> int:
    """Parse an ISO-8601 timestamp into epoch milliseconds.

    A trailing ``Z`` is accepted. Naive timestamps are interpreted as UTC so the
    same string always maps to the same instant.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间不能为空")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "时间格式无效，请使用 ISO-8601，例如 2026-11-13T10:00:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return calendar.timegm(dt.timetuple()) * 1000 + dt.microsecond // 1000


def iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)
