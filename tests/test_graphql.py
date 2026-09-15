"""Tests for the GraphQL client and schema introspection.

The test that matters most here is the one for a 200 response carrying an
``errors`` array. That is GraphQL's sharpest edge for a monitoring job: the
transport succeeded, the status code is fine, and the only sign that nothing
was fetched is a field most code never looks at. Read carelessly it means
"no new leads", which is exactly what a broken monitor should never say.
"""

from __future__ import annotations

import json
import unittest
from typing import ClassVar

import httpx

from lead_monitor.auth import PortalSession
from lead_monitor.clients.graphql import GraphQLError, GraphQLLeadsClient
from lead_monitor.config import Settings
from lead_monitor.introspect import build_query, find_leads_query


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://myh4c.example.com",
        "portal_username": "user",
        "portal_password": "portal-secret",
        "leads_api_path": "/fs1",
        "smtp_host": "smtp.example.com",
        "smtp_username": "mailer@example.com",
        "smtp_password": "smtp-secret",
        "mail_from": "mailer@example.com",
        "mail_to": "ops@example.com",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def client_returning(payload: object, status: int = 200) -> GraphQLLeadsClient:
    """A client whose transport answers with a fixed response."""
    settings = make_settings()
    client = GraphQLLeadsClient(settings, session=PortalSession(cookies={"sid": "x"}))
    client._client.close()

    def handler(request: httpx.Request) -> httpx.Response:
        handler.last_request = request  # type: ignore[attr-defined]
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._handler = handler  # type: ignore[attr-defined]
    return client


LEADS = [
    {"id": "1", "name": "Ana", "email": "ana@e.com", "phone": "600123456", "status": "pre-order"},
    {"id": "2", "name": "Beto", "email": "beto@e.com", "phone": "600654321", "status": "Nurturing"},
]


class TestSuccessfulQuery(unittest.TestCase):
    def test_returns_the_data_envelope(self) -> None:
        with client_returning({"data": {"leads": LEADS}}) as client:
            self.assertEqual(client.execute("query { leads { id } }"), {"leads": LEADS})

    def test_maps_the_records_into_leads(self) -> None:
        with client_returning({"data": {"leads": LEADS}}) as client:
            leads = client.fetch_leads()

        self.assertEqual([lead.external_id for lead in leads], ["1", "2"])
        self.assertEqual(leads[0].status, "pre-order")

    def test_sends_the_standard_graphql_envelope(self) -> None:
        with client_returning({"data": {"leads": []}}) as client:
            client.execute("query Leads { leads { id } }", {"days": 30}, "Leads")
            body = json.loads(client._handler.last_request.content)

        self.assertEqual(body["operationName"], "Leads")
        self.assertEqual(body["variables"], {"days": 30})
        self.assertIn("leads", body["query"])

    def test_posts_to_the_configured_endpoint(self) -> None:
        with client_returning({"data": {"leads": []}}) as client:
            client.execute("query { leads { id } }")
            request = client._handler.last_request

        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://myh4c.example.com/fs1")


class TestErrorsInA200(unittest.TestCase):
    """A failed GraphQL query still returns HTTP 200. This is the trap."""

    def test_an_errors_array_raises_rather_than_reading_as_no_leads(self) -> None:
        payload = {"data": None, "errors": [{"message": "Not authorised"}]}
        with client_returning(payload) as client, self.assertRaises(GraphQLError) as ctx:
            client.execute("query { leads { id } }")

        self.assertIn("Not authorised", str(ctx.exception))

    def test_fetch_leads_propagates_it_instead_of_returning_empty(self) -> None:
        """Returning [] here would silently mean 'nothing new, forever'."""
        payload = {"data": None, "errors": [{"message": "Cannot query field 'leads'"}]}
        with client_returning(payload) as client, self.assertRaises(GraphQLError):
            client.fetch_leads()

    def test_errors_alongside_partial_data_still_raise(self) -> None:
        payload = {"data": {"leads": LEADS[:1]}, "errors": [{"message": "field failed"}]}
        with client_returning(payload) as client, self.assertRaises(GraphQLError):
            client.execute("query { leads { id } }")

    def test_a_response_with_neither_data_nor_errors_raises(self) -> None:
        with client_returning({}) as client, self.assertRaises(GraphQLError):
            client.execute("query { leads { id } }")

    def test_non_json_raises_a_readable_error(self) -> None:
        with (
            client_returning("<html>gateway timeout</html>") as client,
            self.assertRaises(GraphQLError) as ctx,
        ):
            client.execute("query { leads { id } }")
        self.assertIn("did not return JSON", str(ctx.exception))

    def test_a_500_still_raises(self) -> None:
        with (
            client_returning({"data": None}, status=500) as client,
            self.assertRaises(httpx.HTTPStatusError),
        ):
            client.execute("query { leads { id } }")


class TestConfiguredQuery(unittest.TestCase):
    def test_uses_the_configured_query_when_set(self) -> None:
        settings = make_settings(leads_graphql_query="query Custom { leads { id } }")
        client = GraphQLLeadsClient(settings, session=PortalSession(cookies={"sid": "x"}))
        self.assertEqual(client._query(), "query Custom { leads { id } }")
        client.close()

    def test_falls_back_to_the_probed_query(self) -> None:
        settings = make_settings(leads_graphql_query="")
        client = GraphQLLeadsClient(settings, session=PortalSession(cookies={"sid": "x"}))
        query = client._query()
        client.close()

        # The schema is PascalCase except for `id`. Getting either half of that
        # wrong makes the whole query fail validation.
        for field in ("id", "FirstName", "LastName", "Email", "Phone", "Status", "CreatedDate"):
            self.assertIn(field, query)
        self.assertIn("query Leads", query)

    def test_rejects_malformed_variables_clearly(self) -> None:
        settings = make_settings(leads_graphql_variables="{not json")
        client = GraphQLLeadsClient(settings, session=PortalSession(cookies={"sid": "x"}))
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            client._variables()
        client.close()

    def test_rejects_variables_that_are_not_an_object(self) -> None:
        settings = make_settings(leads_graphql_variables="[1, 2]")
        client = GraphQLLeadsClient(settings, session=PortalSession(cookies={"sid": "x"}))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            client._variables()
        client.close()


class TestIntrospection(unittest.TestCase):
    ROOT_FIELDS: ClassVar[list[dict[str, object]]] = [
        {
            "name": "notifications",
            "args": [],
            "type": {"kind": "LIST", "ofType": {"name": "Notification", "kind": "OBJECT"}},
        },
        {
            "name": "leads",
            "args": [{"name": "days", "type": {"name": "Int", "kind": "SCALAR"}}],
            "type": {"kind": "LIST", "ofType": {"name": "Lead", "kind": "OBJECT"}},
        },
        {"name": "me", "args": [], "type": {"name": "User", "kind": "OBJECT"}},
    ]

    def test_picks_the_leads_query_over_its_neighbours(self) -> None:
        found = find_leads_query(self.ROOT_FIELDS)
        assert found is not None
        self.assertEqual(found["name"], "leads")

    def test_prefers_an_exact_name_match(self) -> None:
        fields = [
            {"name": "leadSources", "args": [], "type": {"kind": "LIST", "ofType": {"name": "X"}}},
            {"name": "leads", "args": [], "type": {"kind": "LIST", "ofType": {"name": "Lead"}}},
        ]
        found = find_leads_query(fields)
        assert found is not None
        self.assertEqual(found["name"], "leads")

    def test_returns_nothing_when_no_query_looks_like_leads(self) -> None:
        fields = [{"name": "me", "args": [], "type": {"name": "User"}}]
        self.assertIsNone(find_leads_query(fields))

    def test_builds_a_query_with_arguments(self) -> None:
        query = build_query("leads", ["id", "name"], [{"name": "days", "type": {"name": "Int"}}])
        self.assertIn("query Leads($days: Int)", query)
        self.assertIn("leads(days: $days)", query)

    def test_builds_a_query_without_arguments(self) -> None:
        query = build_query("leads", ["id", "name"], [])
        self.assertIn("query Leads {", query)
        self.assertNotIn("(", query.split("\n")[0])

    def test_drops_typename_noise(self) -> None:
        query = build_query("leads", ["id", "__typename"], [])
        self.assertNotIn("__typename", query)


if __name__ == "__main__":
    unittest.main()
