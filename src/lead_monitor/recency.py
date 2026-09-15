"""Recency window for notification eligibility.

A lead can sit in "pre-order" for weeks without ever converting. The dedup
logic in ``store.py`` only guarantees a lead is never *announced twice* — it
says nothing about whether a lead should be announced at all. Left alone,
the first time the monitor's database meets a months-old stuck lead (a
schema change, a manual reseed, a gap in coverage), it would read as "new"
and go out in an email, which is exactly the backlog noise this module
exists to prevent.

The window comes from Clarita (front-of-house/sales): notify on leads from
the last day. If today is Monday, also look back across the weekend, since
nobody is watching the portal on Saturday or Sunday and a lead that arrived
then is still the first thing worth seeing on Monday morning.

The cutoff is calendar-based in the business's own timezone, not a rolling
24h/48h window in UTC — "yesterday" and "Monday" are local-calendar concepts
to the person who defined the rule, and computing them in UTC would shift
the boundary by however many hours Madrid sits off UTC, including across the
DST change.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")
_MONDAY = 0


def recency_cutoff(now: datetime, *, timezone: str) -> datetime:
    """Return the earliest ``created_at`` (timezone-aware, UTC) still eligible.

    Normally: the start of yesterday, local time — so "yesterday and today".
    On a Monday: the start of Saturday, local time — so the whole weekend
    plus today. ``now`` may be naive (assumed UTC) or aware.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UTC)

    local_now = now.astimezone(ZoneInfo(timezone))
    start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    days_back = 2 if start_of_today.weekday() == _MONDAY else 1
    cutoff_local = start_of_today - timedelta(days=days_back)
    return cutoff_local.astimezone(_UTC)


def is_recent(created_at: datetime | None, *, cutoff: datetime) -> bool:
    """Whether a lead's ``created_at`` falls on or after ``cutoff``.

    ``created_at`` is treated as recent when it is missing entirely. Real
    leads from the GraphQL client always carry ``CreatedDate``, so a missing
    value here means parsing failed on something unexpected, not that the
    lead is old — and understating "new" costs one extra email, while
    overstating "old" costs a silently dropped lead. Naive timestamps (a
    portal format with no offset) are assumed UTC, matching the rest of the
    codebase.
    """
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=_UTC)
    return created_at >= cutoff
