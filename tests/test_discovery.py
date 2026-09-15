"""Tests for endpoint scoring.

The browser capture needs a real portal, but the judgement — which of forty
captured responses is the leads list — is pure logic and worth pinning down,
because getting it wrong sends the whole project down the DOM-scraping path
unnecessarily.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from lead_monitor.discovery import CapturedResponse, _find_record_array, score_response


def response(
    body: Any, url: str = "https://portal.example.com/api/x", **kw: Any
) -> CapturedResponse:
    defaults: dict[str, Any] = {
        "url": url,
        "method": "GET",
        "status": 200,
        "content_type": "application/json",
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }
    defaults.update(kw)
    return CapturedResponse(**defaults)


LEADS = [
    {"id": 1, "name": "Ana", "email": "ana@example.com", "status": "Pre Order"},
    {"id": 2, "name": "Beto", "email": "beto@example.com", "status": "Pre Order"},
]


class TestCollectionLocation(unittest.TestCase):
    def test_finds_a_bare_array(self) -> None:
        records, path = _find_record_array(LEADS)
        assert records is not None
        self.assertEqual(len(records), 2)
        self.assertEqual(path, "")

    def test_finds_a_wrapped_array(self) -> None:
        records, path = _find_record_array({"data": LEADS})
        assert records is not None
        self.assertEqual(path, "data")

    def test_finds_a_graphql_shape(self) -> None:
        records, path = _find_record_array({"data": {"leads": {"edges": LEADS}}})
        assert records is not None
        self.assertEqual(path, "data.leads.edges")

    def test_prefers_the_conventional_key_over_metadata(self) -> None:
        """{"meta": [...], "data": [...]} must report data, not meta."""
        records, path = _find_record_array(
            {"meta": [{"page": 1}], "data": LEADS},
        )
        assert records is not None
        self.assertEqual(path, "data")

    def test_ignores_arrays_of_scalars(self) -> None:
        records, _ = _find_record_array({"tags": ["a", "b", "c"]})
        self.assertIsNone(records)

    def test_gives_up_on_deeply_nested_structures(self) -> None:
        deep: Any = LEADS
        for _ in range(10):
            deep = {"wrap": deep}
        records, _ = _find_record_array(deep)
        self.assertIsNone(records)


class TestScoring(unittest.TestCase):
    def test_a_leads_endpoint_scores_highly(self) -> None:
        candidate = score_response(
            response({"data": LEADS}, url="https://portal.example.com/api/leads")
        )
        assert candidate is not None
        self.assertGreater(candidate.score, 50)
        self.assertEqual(candidate.record_count, 2)

    def test_it_beats_an_unrelated_json_endpoint(self) -> None:
        leads = score_response(response({"data": LEADS}, url="https://p.example.com/api/leads"))
        menu = score_response(
            response(
                {"items": [{"label": "Home", "href": "/"}, {"label": "Clubs", "href": "/c"}]},
                url="https://p.example.com/api/navigation",
            )
        )
        assert leads is not None and menu is not None
        self.assertGreater(leads.score, menu.score)

    def test_the_pre_order_value_is_a_strong_signal(self) -> None:
        with_status = score_response(response({"data": LEADS}, url="https://p.example.com/api/x"))
        without = score_response(
            response(
                {"data": [{"id": 1, "name": "Ana", "email": "a@e.com", "status": "Converted"}]},
                url="https://p.example.com/api/x",
            )
        )
        assert with_status is not None and without is not None
        self.assertGreater(with_status.score, without.score)

    def test_graphql_is_recognised(self) -> None:
        candidate = score_response(
            response(
                {"data": {"leads": LEADS}},
                url="https://p.example.com/graphql",
                is_graphql=True,
            )
        )
        assert candidate is not None
        self.assertIn("GraphQL operation", candidate.reasons)

    def test_spanish_field_names_are_recognised(self) -> None:
        candidate = score_response(
            response(
                {"data": [{"id": 1, "nombre": "Ana", "correo": "a@e.com", "estado": "Pre Order"}]}
            )
        )
        assert candidate is not None
        self.assertTrue(any("lead-like fields" in reason for reason in candidate.reasons))

    def test_reports_the_available_fields(self) -> None:
        candidate = score_response(response({"data": LEADS}))
        assert candidate is not None
        self.assertEqual(candidate.sample_keys, ["email", "id", "name", "status"])


class TestRejection(unittest.TestCase):
    def test_error_responses_are_discarded(self) -> None:
        self.assertIsNone(score_response(response({"data": LEADS}, status=500)))

    def test_non_json_is_discarded(self) -> None:
        self.assertIsNone(score_response(response("<html><body>hi</body></html>")))

    def test_json_without_a_collection_is_discarded(self) -> None:
        self.assertIsNone(score_response(response({"user": {"id": 1, "name": "Ana"}})))


class TestPath(unittest.TestCase):
    def test_strips_the_host_and_query_string(self) -> None:
        captured = response(LEADS, url="https://portal.example.com/api/leads?page=2&status=pre")
        self.assertEqual(captured.path, "/api/leads")


if __name__ == "__main__":
    unittest.main()
