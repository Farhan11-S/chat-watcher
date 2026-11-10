"""Times are stored in UTC (SQLite's datetime('now'), which the job queue compares
against) and shown in one time zone: general.timezone, or this computer's when empty."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from chatwatcher.config import Config


def zone(cfg: Config) -> tzinfo | None:
    """The display time zone. None = this computer's local time (astimezone(None))."""
    name = cfg.general.timezone.strip()
    return ZoneInfo(name) if name else None


def parse_utc(value: Any) -> datetime | None:
    """'2026-11-08 05:51:21' (SQLite, UTC), ISO with Z/offset, or a datetime → aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def abbr(dt: datetime) -> str:
    """'WIB', 'JST', … or 'UTC+07' when the zone has no short name."""
    name = dt.tzname() or ""
    if name and name[0] not in "+-":
        return name
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h:02d}" + (f":{m:02d}" if m else "")


def fmt(value: Any, tz: tzinfo | None, *, date_only: bool = False, seconds: bool = False) -> str:
    """A stored UTC time as local text, e.g. '2026-11-08 12:51 WIB'. Unparseable → as is."""
    dt = parse_utc(value)
    if dt is None:
        return str(value or "")
    local = dt.astimezone(tz)
    if date_only:
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M" + (":%S" if seconds else "")) + " " + abbr(local)


def local_days(start: date, end: date, tz: tzinfo | None) -> tuple[str, str]:
    """The UTC span (SQLite format) of local days start..end, both included: to compare
    with datetime(stored time)."""
    def utc(d: date) -> str:
        midnight = datetime.combine(d, time.min, tzinfo=tz) if tz else datetime.combine(d, time.min).astimezone()
        return midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return utc(start), utc(end + timedelta(days=1))


def today(tz: tzinfo | None) -> date:
    return datetime.now(timezone.utc).astimezone(tz).date()


def zone_name(tz: tzinfo | None) -> str:
    return abbr(datetime.now(timezone.utc).astimezone(tz))
