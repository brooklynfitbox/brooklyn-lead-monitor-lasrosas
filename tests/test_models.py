"""Tests for lead identity, which is what stops duplicate emails."""

from __future__ import annotations

import unittest

from lead_monitor.models import Lead


class TestNormalisation(unittest.TestCase):
    def test_none_becomes_empty_string(self) -> None:
        lead = Lead(external_id="1", name=None, email=None)  # type: ignore[arg-type]
        self.assertEqual(lead.name, "")
        self.assertEqual(lead.email, "")

    def test_whitespace_is_collapsed(self) -> None:
        lead = Lead(external_id="1", name="  Ana   Gómez \n")
        self.assertEqual(lead.name, "Ana Gómez")

    def test_non_string_values_are_coerced(self) -> None:
        lead = Lead(external_id="1", phone=600123456)  # type: ignore[arg-type]
        self.assertEqual(lead.phone, "600123456")


class TestFingerprint(unittest.TestCase):
    def test_ignores_the_portal_identifier(self) -> None:
        """The point of the fingerprint is to survive renumbering."""
        a = Lead(external_id="1", name="Ana", email="ana@example.com", phone="600 123 456")
        b = Lead(external_id="99999", name="Ana", email="ana@example.com", phone="600 123 456")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_ignores_status_changes(self) -> None:
        a = Lead(external_id="1", name="Ana", email="ana@example.com", status="Pre Order")
        b = Lead(external_id="1", name="Ana", email="ana@example.com", status="Converted")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_ignores_phone_formatting(self) -> None:
        a = Lead(external_id="1", name="Ana", phone="+34 600 123 456")
        b = Lead(external_id="1", name="Ana", phone="0034-600123456")
        # Same digits once punctuation and spacing are stripped.
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_is_case_insensitive_on_name_and_email(self) -> None:
        a = Lead(external_id="1", name="Ana Gómez", email="Ana@Example.com")
        b = Lead(external_id="1", name="ana gómez", email="ana@example.com")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_different_people_differ(self) -> None:
        a = Lead(external_id="1", name="Ana", email="ana@example.com")
        b = Lead(external_id="2", name="Beto", email="beto@example.com")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_anonymous_leads_fall_back_to_the_identifier(self) -> None:
        """Otherwise every detail-less lead would collide and be silenced."""
        a = Lead(external_id="1")
        b = Lead(external_id="2")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_is_stable_across_instances(self) -> None:
        a = Lead(external_id="1", name="Ana", email="ana@example.com")
        b = Lead(external_id="1", name="Ana", email="ana@example.com")
        self.assertEqual(a.fingerprint, b.fingerprint)


class TestSummary(unittest.TestCase):
    def test_prefers_the_name(self) -> None:
        lead = Lead(external_id="1", name="Ana", email="ana@example.com", status="Pre Order")
        self.assertEqual(lead.summary(), "Ana (Pre Order)")

    def test_falls_back_through_email_then_phone_then_id(self) -> None:
        self.assertTrue(
            Lead(external_id="1", email="ana@example.com").summary().startswith("ana@example.com")
        )
        self.assertTrue(Lead(external_id="1", phone="600123456").summary().startswith("600123456"))
        self.assertTrue(Lead(external_id="abc").summary().startswith("abc"))

    def test_names_the_missing_status(self) -> None:
        self.assertIn("unknown status", Lead(external_id="1", name="Ana").summary())


if __name__ == "__main__":
    unittest.main()
