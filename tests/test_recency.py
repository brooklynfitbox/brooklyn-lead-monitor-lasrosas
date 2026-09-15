"""Tests for the Clarita recency window, in isolation from the run loop.

Pure logic, deliberately pinned down with explicit calendar dates rather than
"today", because the whole point of the rule is what happens on a Monday —
and a test that only ever runs on whatever day CI happens to execute would
never actually exercise that branch.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from lead_monitor.recency import is_recent, recency_cutoff

MADRID = "Europe/Madrid"


def madrid(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(MADRID))


class TestCutoffOnAnOrdinaryDay(unittest.TestCase):
    """2026-08-04 is a Tuesday: the plain "last day" rule applies."""

    def test_cutoff_is_the_start_of_yesterday_local(self) -> None:
        now = madrid(2026, 8, 4, hour=15, minute=30)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertEqual(cutoff.astimezone(ZoneInfo(MADRID)), madrid(2026, 8, 3, hour=0, minute=0))

    def test_a_lead_from_this_morning_is_recent(self) -> None:
        now = madrid(2026, 8, 4, hour=15)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertTrue(is_recent(madrid(2026, 8, 4, hour=8), cutoff=cutoff))

    def test_a_lead_from_yesterday_afternoon_is_recent(self) -> None:
        now = madrid(2026, 8, 4, hour=15)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertTrue(is_recent(madrid(2026, 8, 3, hour=18), cutoff=cutoff))

    def test_a_lead_from_the_day_before_yesterday_is_stale(self) -> None:
        now = madrid(2026, 8, 4, hour=15)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertFalse(is_recent(madrid(2026, 8, 2, hour=23, minute=59), cutoff=cutoff))


class TestCutoffOnMonday(unittest.TestCase):
    """2026-08-03 is a Monday: Clarita's rule reaches back across the weekend."""

    def test_cutoff_is_the_start_of_saturday_local(self) -> None:
        now = madrid(2026, 8, 3, hour=9)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertEqual(cutoff.astimezone(ZoneInfo(MADRID)), madrid(2026, 8, 1, hour=0, minute=0))

    def test_a_lead_from_saturday_morning_is_recent(self) -> None:
        now = madrid(2026, 8, 3, hour=9)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertTrue(is_recent(madrid(2026, 8, 1, hour=10), cutoff=cutoff))

    def test_a_lead_from_sunday_is_recent(self) -> None:
        now = madrid(2026, 8, 3, hour=9)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertTrue(is_recent(madrid(2026, 8, 2, hour=20), cutoff=cutoff))

    def test_a_lead_from_last_friday_is_still_stale(self) -> None:
        """The window covers the weekend, not an extra work day before it."""
        now = madrid(2026, 8, 3, hour=9)
        cutoff = recency_cutoff(now, timezone=MADRID)
        self.assertFalse(is_recent(madrid(2026, 7, 31, hour=23), cutoff=cutoff))


class TestIsRecentEdgeCases(unittest.TestCase):
    def test_missing_created_at_is_treated_as_recent(self) -> None:
        cutoff = recency_cutoff(madrid(2026, 8, 4), timezone=MADRID)
        self.assertTrue(is_recent(None, cutoff=cutoff))

    def test_exactly_at_the_cutoff_counts_as_recent(self) -> None:
        cutoff = recency_cutoff(madrid(2026, 8, 4), timezone=MADRID)
        self.assertTrue(is_recent(cutoff, cutoff=cutoff))

    def test_a_naive_timestamp_is_assumed_utc(self) -> None:
        now = datetime(2026, 8, 4, 15, tzinfo=UTC)
        cutoff = recency_cutoff(now, timezone=MADRID)
        naive_recent = datetime(2026, 8, 4, 6, 0)  # naive, would be recent as UTC
        self.assertTrue(is_recent(naive_recent, cutoff=cutoff))

    def test_a_naive_now_is_assumed_utc(self) -> None:
        naive_now = datetime(2026, 8, 4, 15, 0)
        aware_now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
        self.assertEqual(
            recency_cutoff(naive_now, timezone=MADRID),
            recency_cutoff(aware_now, timezone=MADRID),
        )


if __name__ == "__main__":
    unittest.main()
