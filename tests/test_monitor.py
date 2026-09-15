"""Tests for the run loop, with fake collaborators.

These are the tests that would have caught the classic mistake in a monitor like
this: marking leads as notified before the email actually went out.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lead_monitor.config import Settings
from lead_monitor.models import Lead
from lead_monitor.monitor import run_once
from lead_monitor.store import LeadStore

logging.getLogger("lead_monitor").setLevel(logging.CRITICAL)


class FakeClient:
    def __init__(self, leads: Sequence[Lead], fail: Exception | None = None) -> None:
        self.leads = list(leads)
        self.fail = fail
        self.closed = False
        self.calls = 0

    def fetch_leads(self) -> list[Lead]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return list(self.leads)

    def close(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self, fail: Exception | None = None) -> None:
        self.batches: list[list[Lead]] = []
        self.fail = fail

    def send(self, leads: Sequence[Lead]) -> None:
        if self.fail is not None:
            raise self.fail
        self.batches.append(list(leads))


def lead(n: int, status: str = "Pre Order", created_at: datetime | None = None) -> Lead:
    return Lead(
        external_id=str(n),
        name=f"Lead {n}",
        email=f"lead{n}@example.com",
        phone=f"60000000{n}",
        status=status,
        # Recent by default: most tests exercise dedup and notify-once
        # behaviour, not the recency window itself, and NOTIFY_RECENT_LEADS_ONLY
        # defaults to on. TestRecencyFilter below passes explicit old timestamps.
        created_at=created_at if created_at is not None else datetime.now(UTC),
    )


class MonitorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = LeadStore(self.tmp / "leads.db")
        self.store.connect()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "portal_base_url": "https://portal.example.com",
            "portal_username": "user",
            "portal_password": "portal-secret",
            "smtp_host": "smtp.example.com",
            "smtp_username": "mailer@example.com",
            "smtp_password": "smtp-secret",
            "mail_from": "mailer@example.com",
            "mail_to": "ops@example.com",
            "database_path": self.tmp / "leads.db",
            "seed_without_notifying": False,
            # Explicit so these tests are independent of the shipped default.
            "leads_status_filter": "pre-order",
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    def run_with(self, client: FakeClient, notifier: FakeNotifier, **overrides: object):
        return run_once(
            self.settings(**overrides), client=client, notifier=notifier, store=self.store
        )


class TestFilterModes(MonitorTestCase):
    def test_default_notifies_only_pre_order_leads(self) -> None:
        """The shipped default: a lead already moved to Nurturing is skipped."""
        notifier = FakeNotifier()
        outcome = self.run_with(
            FakeClient([lead(1, status="pre-order"), lead(2, status="Nurturing")]),
            notifier,
            leads_status_filter="pre-order",
        )
        self.assertEqual(outcome.matched_filter, 1)
        self.assertEqual([x.external_id for x in notifier.batches[0]], ["1"])

    def test_blank_filter_announces_every_new_lead(self) -> None:
        """Opt-out: a lead already moved to Nurturing must still be reported."""
        notifier = FakeNotifier()
        outcome = self.run_with(
            FakeClient(
                [
                    lead(1, status="Pre Order"),
                    lead(2, status="Nurturing"),
                    lead(3, status="order-failed"),
                ]
            ),
            notifier,
            leads_status_filter="",
        )

        self.assertEqual(outcome.matched_filter, 3)
        self.assertEqual(outcome.notified, 3)

    def test_the_status_still_reaches_the_notification(self) -> None:
        notifier = FakeNotifier()
        self.run_with(FakeClient([lead(1, status="Nurturing")]), notifier, leads_status_filter="")
        self.assertEqual(notifier.batches[0][0].status, "Nurturing")


class TestFiltering(MonitorTestCase):
    def test_only_leads_in_the_wanted_status_are_recorded(self) -> None:
        client = FakeClient([lead(1), lead(2, status="Converted"), lead(3)])
        notifier = FakeNotifier()

        outcome = self.run_with(client, notifier)

        self.assertEqual(outcome.fetched, 3)
        self.assertEqual(outcome.matched_filter, 2)
        self.assertEqual(outcome.newly_recorded, 2)

    def test_status_spelling_variations_still_match(self) -> None:
        client = FakeClient([lead(1, status="pre-order"), lead(2, status="PREORDER")])
        outcome = self.run_with(client, FakeNotifier())
        self.assertEqual(outcome.matched_filter, 2)


class TestNotification(MonitorTestCase):
    def test_new_leads_are_emailed_once(self) -> None:
        notifier = FakeNotifier()
        outcome = self.run_with(FakeClient([lead(1), lead(2)]), notifier)

        self.assertEqual(outcome.notified, 2)
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(notifier.batches[0]), 2)

    def test_a_second_run_with_the_same_leads_sends_nothing(self) -> None:
        """The requirement that matters most: never notify twice."""
        first = FakeNotifier()
        self.run_with(FakeClient([lead(1), lead(2)]), first)

        second = FakeNotifier()
        outcome = self.run_with(FakeClient([lead(1), lead(2)]), second)

        self.assertEqual(outcome.newly_recorded, 0)
        self.assertEqual(outcome.notified, 0)
        self.assertEqual(second.batches, [])

    def test_only_the_genuinely_new_lead_is_announced(self) -> None:
        self.run_with(FakeClient([lead(1)]), FakeNotifier())

        notifier = FakeNotifier()
        self.run_with(FakeClient([lead(1), lead(2)]), notifier)

        self.assertEqual([x.external_id for x in notifier.batches[0]], ["2"])

    def test_nothing_is_sent_when_there_is_nothing_new(self) -> None:
        notifier = FakeNotifier()
        self.run_with(FakeClient([]), notifier)
        self.assertEqual(notifier.batches, [])


class TestFailureHandling(MonitorTestCase):
    def test_a_failed_send_leaves_the_lead_queued_for_the_next_run(self) -> None:
        failing = FakeNotifier(fail=OSError("smtp down"))
        outcome = self.run_with(FakeClient([lead(1)]), failing)

        self.assertEqual(outcome.notified, 0)
        self.assertFalse(outcome.ok)

        # Next run: the portal no longer reports it as new, but it must still go out.
        working = FakeNotifier()
        second = self.run_with(FakeClient([lead(1)]), working)

        self.assertEqual(second.newly_recorded, 0)
        self.assertEqual(second.notified, 1)
        self.assertEqual([x.external_id for x in working.batches[0]], ["1"])

    def test_a_fetch_failure_is_recorded_without_raising(self) -> None:
        """A scheduled run should log and exit non-zero, not explode."""
        outcome = self.run_with(FakeClient([], fail=ConnectionError("portal down")), FakeNotifier())

        self.assertFalse(outcome.ok)
        self.assertIn("ConnectionError", outcome.errors[0])

    def test_a_fetch_failure_sends_nothing(self) -> None:
        notifier = FakeNotifier()
        self.run_with(FakeClient([], fail=ConnectionError("down")), notifier)
        self.assertEqual(notifier.batches, [])

    def test_the_run_is_always_recorded(self) -> None:
        self.run_with(FakeClient([], fail=ConnectionError("down")), FakeNotifier())
        row = self.store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["ok"], 0)


class TestColdStart(MonitorTestCase):
    def test_the_first_run_seeds_silently(self) -> None:
        notifier = FakeNotifier()
        outcome = self.run_with(
            FakeClient([lead(i) for i in range(1, 41)]),
            notifier,
            seed_without_notifying=True,
        )

        self.assertTrue(outcome.seeded)
        self.assertEqual(outcome.newly_recorded, 40)
        self.assertEqual(outcome.notified, 0)
        self.assertEqual(notifier.batches, [])

    def test_leads_arriving_after_the_seed_are_announced(self) -> None:
        self.run_with(FakeClient([lead(1), lead(2)]), FakeNotifier(), seed_without_notifying=True)

        notifier = FakeNotifier()
        outcome = self.run_with(
            FakeClient([lead(1), lead(2), lead(3)]), notifier, seed_without_notifying=True
        )

        self.assertFalse(outcome.seeded)
        self.assertEqual(outcome.notified, 1)
        self.assertEqual([x.external_id for x in notifier.batches[0]], ["3"])

    def test_seeding_can_be_switched_off(self) -> None:
        notifier = FakeNotifier()
        outcome = self.run_with(FakeClient([lead(1)]), notifier, seed_without_notifying=False)
        self.assertFalse(outcome.seeded)
        self.assertEqual(outcome.notified, 1)


class TestRecencyFilter(MonitorTestCase):
    """A pre-order lead that never converted must not resurface as 'new'."""

    def test_a_lead_created_today_is_notified(self) -> None:
        notifier = FakeNotifier()
        outcome = self.run_with(FakeClient([lead(1, created_at=datetime.now(UTC))]), notifier)
        self.assertEqual(outcome.notified, 1)
        self.assertEqual(outcome.recency_excluded, 0)

    def test_a_lead_from_last_week_is_recorded_but_not_notified(self) -> None:
        stale = lead(1, created_at=datetime.now(UTC) - timedelta(days=10))
        notifier = FakeNotifier()

        outcome = self.run_with(FakeClient([stale]), notifier)

        self.assertEqual(outcome.recency_excluded, 1)
        self.assertEqual(outcome.notified, 0)
        self.assertEqual(notifier.batches, [])
        # It must still be recorded, so it's never mistaken for new later.
        self.assertIn("1", self.store.known_ids())

    def test_a_stale_lead_is_never_notified_on_a_later_run_either(self) -> None:
        stale = lead(1, created_at=datetime.now(UTC) - timedelta(days=10))
        self.run_with(FakeClient([stale]), FakeNotifier())

        notifier = FakeNotifier()
        outcome = self.run_with(FakeClient([stale]), notifier)

        self.assertEqual(outcome.newly_recorded, 0)
        self.assertEqual(outcome.notified, 0)
        self.assertEqual(notifier.batches, [])

    def test_a_fresh_lead_still_gets_through_alongside_a_stale_one(self) -> None:
        stale = lead(1, created_at=datetime.now(UTC) - timedelta(days=10))
        fresh = lead(2, created_at=datetime.now(UTC))
        notifier = FakeNotifier()

        outcome = self.run_with(FakeClient([stale, fresh]), notifier)

        self.assertEqual(outcome.recency_excluded, 1)
        self.assertEqual(outcome.notified, 1)
        self.assertEqual([x.external_id for x in notifier.batches[0]], ["2"])

    def test_the_filter_can_be_switched_off(self) -> None:
        stale = lead(1, created_at=datetime.now(UTC) - timedelta(days=10))
        notifier = FakeNotifier()

        outcome = self.run_with(FakeClient([stale]), notifier, notify_recent_leads_only=False)

        self.assertEqual(outcome.recency_excluded, 0)
        self.assertEqual(outcome.notified, 1)

    def test_a_missing_created_at_is_not_treated_as_stale(self) -> None:
        # lead() defaults created_at to now, so build the Lead directly here
        # to get a genuinely absent timestamp.
        no_timestamp = Lead(
            external_id="1",
            name="Lead 1",
            email="l1@example.com",
            phone="600000001",
            status="Pre Order",
            created_at=None,
        )
        notifier = FakeNotifier()

        outcome = self.run_with(FakeClient([no_timestamp]), notifier)

        self.assertEqual(outcome.recency_excluded, 0)
        self.assertEqual(outcome.notified, 1)


class TestPersonalDataWiring(MonitorTestCase):
    """run_once must build its own store with the redaction setting, not just
    accept whatever an injected store happens to be configured with — this is
    what actually protects the database committed to git in production."""

    def test_the_database_run_once_builds_itself_is_redacted_by_default(self) -> None:
        # notify_include_personal_data defaults to False.
        settings = self.settings(seed_without_notifying=False)
        run_once(
            settings, client=FakeClient([lead(1, status="Pre Order")]), notifier=FakeNotifier()
        )

        with LeadStore(settings.database_path) as reopened:
            row = reopened.conn.execute(
                "SELECT name, email, raw FROM leads WHERE external_id = '1'"
            ).fetchone()
        self.assertEqual(row["name"], "")
        self.assertEqual(row["email"], "")
        self.assertEqual(row["raw"], "{}")

    def test_opting_in_stores_the_full_record(self) -> None:
        settings = self.settings(seed_without_notifying=False, notify_include_personal_data=True)
        client = FakeClient([lead(1, status="Pre Order")])
        run_once(settings, client=client, notifier=FakeNotifier())

        with LeadStore(settings.database_path) as reopened:
            row = reopened.conn.execute(
                "SELECT name, email FROM leads WHERE external_id = '1'"
            ).fetchone()
        self.assertEqual(row["name"], "Lead 1")
        self.assertEqual(row["email"], "lead1@example.com")


class TestColdStartRecency(MonitorTestCase):
    def test_cold_start_seeding_takes_priority_over_recency(self) -> None:
        """The first run is silenced by seeding, not by the recency window."""
        old_and_new = [
            lead(1, created_at=datetime.now(UTC) - timedelta(days=30)),
            lead(2, created_at=datetime.now(UTC)),
        ]
        notifier = FakeNotifier()

        outcome = self.run_with(FakeClient(old_and_new), notifier, seed_without_notifying=True)

        self.assertTrue(outcome.seeded)
        self.assertEqual(outcome.newly_recorded, 2)
        self.assertEqual(outcome.notified, 0)
        self.assertEqual(outcome.recency_excluded, 0)


if __name__ == "__main__":
    unittest.main()
