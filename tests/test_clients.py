"""Tests for record mapping and table parsing.

Neither client's transport is exercised here — those need a portal. What is
tested is the translation layer, which is where a schema surprise turns into
either a wrong lead or a crash.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import ClassVar

from lead_monitor.clients.base import (
    iter_records,
    matches_status,
    parse_datetime,
    record_to_lead,
    records_to_leads,
)
from lead_monitor.clients.dom import parse_table
from lead_monitor.models import Lead


class TestFieldMapping(unittest.TestCase):
    def test_maps_english_field_names(self) -> None:
        lead = record_to_lead(
            {"id": 7, "name": "Ana", "email": "a@e.com", "status": "Pre Order", "club": "Madrid"}
        )
        assert lead is not None
        self.assertEqual(lead.external_id, "7")
        self.assertEqual(lead.name, "Ana")
        self.assertEqual(lead.club, "Madrid")

    def test_maps_spanish_field_names(self) -> None:
        lead = record_to_lead(
            {"id": 7, "nombre": "Ana", "correo": "a@e.com", "estado": "Pre Order", "centro": "Sol"}
        )
        assert lead is not None
        self.assertEqual(lead.name, "Ana")
        self.assertEqual(lead.email, "a@e.com")
        self.assertEqual(lead.club, "Sol")

    def test_field_lookup_is_case_insensitive(self) -> None:
        lead = record_to_lead({"ID": 7, "Name": "Ana", "EMAIL": "a@e.com"})
        assert lead is not None
        self.assertEqual(lead.external_id, "7")
        self.assertEqual(lead.name, "Ana")

    def test_prefers_the_first_candidate_key(self) -> None:
        lead = record_to_lead({"id": "primary", "uuid": "secondary", "name": "Ana"})
        assert lead is not None
        self.assertEqual(lead.external_id, "primary")

    def test_keeps_the_raw_record(self) -> None:
        record = {"id": 7, "name": "Ana", "somethingElse": 42}
        lead = record_to_lead(record)
        assert lead is not None
        self.assertEqual(lead.raw["somethingElse"], 42)

    def test_a_record_without_an_identifier_is_skipped(self) -> None:
        """Un-deduplicable leads would mean an email every run, forever."""
        self.assertIsNone(record_to_lead({"name": "Ana", "email": "a@e.com"}))

    def test_a_bad_record_does_not_take_the_batch_down(self) -> None:
        leads = records_to_leads([{"id": 1, "name": "Ana"}, {"name": "no id"}])
        self.assertEqual(len(leads), 1)


class TestTimestamps(unittest.TestCase):
    def test_parses_iso_with_a_z_suffix(self) -> None:
        parsed = parse_datetime("2026-07-29T10:00:00Z")
        assert parsed is not None
        self.assertEqual(parsed.year, 2026)
        self.assertIsNotNone(parsed.tzinfo)

    def test_parses_spanish_day_first_dates(self) -> None:
        parsed = parse_datetime("29/07/2026 10:30")
        assert parsed is not None
        self.assertEqual((parsed.day, parsed.month), (29, 7))

    def test_parses_epoch_milliseconds(self) -> None:
        parsed = parse_datetime(1_785_000_000_000)
        assert parsed is not None
        self.assertGreater(parsed.year, 2020)

    def test_returns_none_for_junk(self) -> None:
        self.assertIsNone(parse_datetime("not a date"))
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))

    def test_passes_a_datetime_through(self) -> None:
        now = datetime(2026, 7, 29)
        self.assertEqual(parse_datetime(now), now)


class TestStatusMatching(unittest.TestCase):
    def test_no_filter_accepts_everything(self) -> None:
        """The default. 'Pre Order' is transient, so filtering on it loses
        any lead a member of staff picks up between two polls."""
        for status in ("Pre Order", "Nurturing", "order-failed", ""):
            self.assertTrue(matches_status(Lead(external_id="1", status=status), ""))

    def test_whitespace_only_filter_is_also_no_filter(self) -> None:
        self.assertTrue(matches_status(Lead(external_id="1", status="Nurturing"), "   "))

    def test_ignores_spacing_case_and_punctuation(self) -> None:
        for spelling in ("Pre Order", "pre-order", "PREORDER", "pre_order"):
            self.assertTrue(matches_status(Lead(external_id="1", status=spelling), "Pre Order"))

    def test_rejects_a_different_status_when_filtering(self) -> None:
        self.assertFalse(matches_status(Lead(external_id="1", status="Nurturing"), "Pre Order"))

    def test_an_empty_status_does_not_match_an_explicit_filter(self) -> None:
        self.assertFalse(matches_status(Lead(external_id="1"), "Pre Order"))


class TestEnvelopes(unittest.TestCase):
    def test_unwraps_common_shapes(self) -> None:
        for envelope in (
            [{"id": 1}],
            {"data": [{"id": 1}]},
            {"results": [{"id": 1}]},
            {"data": {"leads": {"edges": [{"id": 1}]}}},
        ):
            self.assertEqual(len(list(iter_records(envelope))), 1, envelope)


class TestTableParsing(unittest.TestCase):
    TABLE: ClassVar[dict[str, object]] = {
        "headers": ["Nombre", "Correo", "Teléfono", "Estado", "Centro", "Fecha alta"],
        "rows": [
            ["Ana Gómez", "ana@e.com", "600123456", "Pre Order", "Madrid", "29/07/2026"],
            ["Beto Ruiz", "beto@e.com", "600654321", "Converted", "Sol", "28/07/2026"],
        ],
    }

    def test_reads_rows_by_header_meaning(self) -> None:
        leads = parse_table(self.TABLE)
        self.assertEqual(len(leads), 2)
        self.assertEqual(leads[0].name, "Ana Gómez")
        self.assertEqual(leads[0].status, "Pre Order")
        self.assertEqual(leads[0].club, "Madrid")

    def test_an_inserted_column_does_not_shift_every_field(self) -> None:
        """The reason columns are keyed by header text rather than position."""
        shifted = {
            "headers": ["#", "Nombre", "Correo", "Teléfono", "Estado"],
            "rows": [["1", "Ana Gómez", "ana@e.com", "600123456", "Pre Order"]],
        }
        leads = parse_table(shifted)
        self.assertEqual(leads[0].name, "Ana Gómez")
        self.assertEqual(leads[0].email, "ana@e.com")

    def test_identity_falls_back_to_contact_details(self) -> None:
        leads = parse_table(self.TABLE)
        self.assertEqual(leads[0].external_id, "ana@e.com")

    def test_an_empty_table_yields_nothing(self) -> None:
        self.assertEqual(parse_table({"headers": [], "rows": []}), [])

    def test_unrecognisable_headers_yield_nothing_rather_than_garbage(self) -> None:
        leads = parse_table({"headers": ["aaa", "bbb"], "rows": [["1", "2"]]})
        self.assertEqual(leads, [])


if __name__ == "__main__":
    unittest.main()


class TestPortalSchemaMapping(unittest.TestCase):
    """The real shape, probed from the live endpoint on 2026-07-29.

    PascalCase everywhere except `id`, and no single name field — two details
    that would each have produced silently wrong leads.
    """

    RECORD: ClassVar[dict[str, object]] = {
        "id": "a1b2c3",
        "FirstName": "Rosalinda Mónica",
        "LastName": "Ojeda Gil",
        "Email": "orosalinda395@example.com",
        "Phone": "+34604348774",
        "Status": "pre-order",
        "CreatedDate": "2026-07-29T00:13:31Z",
        "LastActivityDate": None,
        "LastModifiedDate": "2026-07-29T17:22:36Z",
    }

    def test_maps_every_field(self) -> None:
        lead = record_to_lead(self.RECORD)
        assert lead is not None
        self.assertEqual(lead.external_id, "a1b2c3")
        self.assertEqual(lead.email, "orosalinda395@example.com")
        self.assertEqual(lead.phone, "+34604348774")
        self.assertEqual(lead.status, "pre-order")

    def test_joins_the_split_name(self) -> None:
        """A single-field lookup would leave every notification as '(no name)'."""
        lead = record_to_lead(self.RECORD)
        assert lead is not None
        self.assertEqual(lead.name, "Rosalinda Mónica Ojeda Gil")

    def test_reads_the_pascal_case_timestamp(self) -> None:
        lead = record_to_lead(self.RECORD)
        assert lead is not None
        assert lead.created_at is not None
        self.assertEqual(lead.created_at.year, 2026)

    def test_a_lead_with_only_a_first_name(self) -> None:
        lead = record_to_lead({"id": "1", "FirstName": "Ana", "LastName": None})
        assert lead is not None
        self.assertEqual(lead.name, "Ana")

    def test_a_lead_with_no_name_at_all_is_still_recorded(self) -> None:
        lead = record_to_lead({"id": "1", "Email": "x@e.com"})
        assert lead is not None
        self.assertEqual(lead.name, "")

    def test_the_status_filter_matches_the_portal_spelling(self) -> None:
        lead = record_to_lead(self.RECORD)
        assert lead is not None
        # The shipped default is "pre-order"; the portal spells the status the
        # same way. Both must agree, punctuation and case notwithstanding.
        self.assertTrue(matches_status(lead, "pre-order"))
        self.assertTrue(matches_status(lead, "Pre Order"))
        self.assertFalse(matches_status(lead, "Nurturing"))
