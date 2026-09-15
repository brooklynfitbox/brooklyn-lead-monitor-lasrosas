"""Tests for the persistence guarantees the whole design rests on.

Written around failure windows rather than happy paths, because the happy path
was never the risk: the risk is a run that dies halfway and either re-announces
a lead or forgets one.

These use ``unittest.TestCase`` so they run under the standard library alone as
well as under pytest in CI.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lead_monitor.models import Lead, RunOutcome
from lead_monitor.store import LeadStore


def make_lead(external_id: str, **overrides: object) -> Lead:
    defaults: dict[str, object] = {
        "external_id": external_id,
        "name": f"Lead {external_id}",
        "email": f"lead{external_id}@example.com",
        "phone": f"+3460000{external_id}",
        "status": "Pre Order",
        "club": "Brooklyn Madrid",
        "created_at": datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Lead(**defaults)  # type: ignore[arg-type]


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "leads.db"
        self.store = LeadStore(self.path)
        self.store.connect()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()


class TestRecording(StoreTestCase):
    def test_new_database_is_empty(self) -> None:
        self.assertTrue(self.store.is_empty())
        self.assertEqual(self.store.count(), 0)

    def test_records_only_unseen_leads(self) -> None:
        first = self.store.record_seen([make_lead("1"), make_lead("2")])
        self.assertEqual([lead.external_id for lead in first], ["1", "2"])

        second = self.store.record_seen([make_lead("1"), make_lead("2"), make_lead("3")])
        self.assertEqual([lead.external_id for lead in second], ["3"])
        self.assertEqual(self.store.count(), 3)

    def test_recorded_leads_start_unnotified(self) -> None:
        self.store.record_seen([make_lead("1")])
        pending = self.store.pending_notifications()
        self.assertEqual([lead.external_id for lead in pending], ["1"])

    def test_pending_order_is_oldest_first(self) -> None:
        self.store.record_seen([make_lead("2")])
        self.store.record_seen([make_lead("1")])
        pending = self.store.pending_notifications()
        self.assertEqual([lead.external_id for lead in pending], ["2", "1"])


class TestDeliveryGuarantees(StoreTestCase):
    def test_marking_notified_clears_the_queue(self) -> None:
        self.store.record_seen([make_lead("1"), make_lead("2")])
        self.assertEqual(self.store.mark_notified(["1", "2"]), 2)
        self.assertEqual(self.store.pending_notifications(), [])

    def test_crash_between_record_and_send_retries_next_run(self) -> None:
        """Recorded, then the process dies before the email goes out.

        The next run must still see it as pending, even though the portal will
        no longer report it as new.
        """
        self.store.record_seen([make_lead("1")])
        # ...crash here, no mark_notified call...

        # Next run: the portal offers the same lead, which is no longer "new".
        self.assertEqual(self.store.record_seen([make_lead("1")]), [])

        # But it is still queued, so nothing is lost.
        pending = self.store.pending_notifications()
        self.assertEqual([lead.external_id for lead in pending], ["1"])

    def test_send_failure_leaves_lead_queued_and_counts_the_attempt(self) -> None:
        self.store.record_seen([make_lead("1")])
        self.store.record_notify_failure(["1"])

        pending = self.store.pending_notifications()
        self.assertEqual([lead.external_id for lead in pending], ["1"])

        row = self.store.conn.execute(
            "SELECT notify_failures FROM leads WHERE external_id = '1'"
        ).fetchone()
        self.assertEqual(row["notify_failures"], 1)

    def test_marking_is_idempotent(self) -> None:
        self.store.record_seen([make_lead("1")])
        self.assertEqual(self.store.mark_notified(["1"]), 1)
        self.assertEqual(self.store.mark_notified(["1"]), 0)

    def test_marking_an_unknown_id_is_harmless(self) -> None:
        self.assertEqual(self.store.mark_notified(["does-not-exist"]), 0)

    def test_marking_nothing_is_harmless(self) -> None:
        self.assertEqual(self.store.mark_notified([]), 0)


class TestColdStart(StoreTestCase):
    def test_seeding_records_without_queuing(self) -> None:
        """Capture the existing backlog without emailing about it."""
        self.store.record_seen([make_lead(str(i)) for i in range(1, 51)], already_notified=True)

        self.assertEqual(self.store.count(), 50)
        self.assertEqual(self.store.pending_notifications(), [])
        self.assertFalse(self.store.is_empty())

    def test_lead_arriving_after_the_seed_is_still_notified(self) -> None:
        self.store.record_seen([make_lead("1")], already_notified=True)
        newly = self.store.record_seen([make_lead("1"), make_lead("2")])

        self.assertEqual([lead.external_id for lead in newly], ["2"])
        pending = self.store.pending_notifications()
        self.assertEqual([lead.external_id for lead in pending], ["2"])


class TestIdentity(StoreTestCase):
    def test_fingerprint_guards_against_recycled_identifiers(self) -> None:
        """Same person, different portal id, must not produce a second email."""
        self.store.record_seen([make_lead("1", name="Ana Gómez", email="ana@example.com")])

        duplicate = Lead(
            external_id="9999",
            name="Ana Gómez",
            email="ana@example.com",
            phone="+34600001",
            status="Pre Order",
        )
        self.assertEqual(self.store.record_seen([duplicate]), [])

    def test_whitespace_differences_do_not_create_a_second_lead(self) -> None:
        self.store.record_seen([make_lead("1", name="Ana  Gómez")])
        self.assertEqual(self.store.record_seen([make_lead("1", name="Ana Gómez  ")]), [])

    def test_genuinely_different_people_are_both_recorded(self) -> None:
        self.store.record_seen([make_lead("1")])
        newly = self.store.record_seen([make_lead("2")])
        self.assertEqual([lead.external_id for lead in newly], ["2"])


class TestPersonalDataRedaction(unittest.TestCase):
    """The database is committed to git; ``store_personal_data`` keeps it clean.

    This mirrors the email's own redact-by-default behaviour (see
    notifier.py) — production wires both to the same setting, so a repo
    configured for redacted emails never has plaintext contact details
    sitting in its git history either.
    """

    def test_off_by_default_leaves_no_contact_details_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path, store_personal_data=False) as store:
                store.record_seen(
                    [make_lead("1", name="Ana Gómez", email="ana@example.com", phone="600111222")]
                )
                row = store.conn.execute("SELECT * FROM leads WHERE external_id = '1'").fetchone()

        self.assertEqual(row["name"], "")
        self.assertEqual(row["email"], "")
        self.assertEqual(row["phone"], "")
        self.assertEqual(row["raw"], "{}")

    def test_redaction_does_not_break_dedup_or_notification(self) -> None:
        """The whole point: privacy on, but nobody gets emailed twice either."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path, store_personal_data=False) as store:
                store.record_seen([make_lead("1", name="Ana Gómez", email="ana@example.com")])
                # Same person, portal renumbered the id: fingerprint still catches it.
                duplicate = Lead(
                    external_id="9999",
                    name="Ana Gómez",
                    email="ana@example.com",
                    phone="+34600001",
                    status="Pre Order",
                )
                self.assertEqual(store.record_seen([duplicate]), [])

                pending = store.pending_notifications()
                self.assertEqual([lead.external_id for lead in pending], ["1"])
                self.assertEqual(pending[0].name, "")

    def test_on_keeps_the_previous_behaviour(self) -> None:
        """Explicit opt-in (or a direct construction, e.g. in a test) is unaffected."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path, store_personal_data=True) as store:
                store.record_seen([make_lead("1", name="Ana Gómez", email="ana@example.com")])
                pending = store.pending_notifications()

        self.assertEqual(pending[0].name, "Ana Gómez")
        self.assertEqual(pending[0].email, "ana@example.com")

    def test_default_construction_also_keeps_the_previous_behaviour(self) -> None:
        """Backward compatibility: existing direct constructions (mostly tests)
        didn't pass this parameter and must keep working exactly as before."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path) as store:
                store.record_seen([make_lead("1", name="Ana Gómez")])
                pending = store.pending_notifications()

        self.assertEqual(pending[0].name, "Ana Gómez")


class TestPersistence(unittest.TestCase):
    def test_state_survives_reopening_the_file(self) -> None:
        """The database is committed and checked out between runs; it must reload."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"

            with LeadStore(path) as first:
                first.record_seen([make_lead("1")])
                first.mark_notified(["1"])

            with LeadStore(path) as second:
                self.assertEqual(second.count(), 1)
                self.assertEqual(second.pending_notifications(), [])
                self.assertEqual(second.record_seen([make_lead("1")]), [])

    def test_database_is_a_single_file(self) -> None:
        """No -wal/-shm siblings, because the file is committed to git."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path) as store:
                store.record_seen([make_lead("1")])
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["leads.db"])

    def test_refuses_a_database_from_the_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leads.db"
            with LeadStore(path) as store:
                store.conn.execute("UPDATE schema_version SET version = 99")

            with (
                self.assertRaisesRegex(RuntimeError, "newer than this code"),
                LeadStore(path),
            ):
                pass

    def test_run_history_is_recorded(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            LeadStore(Path(tmp) / "leads.db") as store,
        ):
            store.record_run(RunOutcome(fetched=10, newly_recorded=2, notified=2))
            row = store.conn.execute("SELECT * FROM runs").fetchone()
            self.assertEqual(row["fetched"], 10)
            self.assertEqual(row["ok"], 1)


if __name__ == "__main__":
    unittest.main()
