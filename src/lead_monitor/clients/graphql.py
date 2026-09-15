"""Fetch leads from the portal's GraphQL endpoint.

The portal exposes one endpoint, ``POST /fs1``, which takes the standard
GraphQL envelope — ``{"operationName", "variables", "query"}`` — and answers
with ``{"data": ..., "errors": ...}``. Authentication is the session cookie
captured by :mod:`lead_monitor.auth`.

GraphQL brings one trap that ordinary REST does not, and it is the reason this
module exists rather than reusing the plain HTTP client: **a failed GraphQL
query still returns HTTP 200**. A permission error, an unknown field, a schema
change after a redeploy — all of them arrive as a cheerful 200 with an
``errors`` array and a null ``data``. Code that only checks the status code
reads that as "zero leads today" and stays quiet forever. So the response is
inspected for ``errors`` before anything else, and a query that failed raises
rather than returning an empty list.

The query itself is configuration, not code. The exact field names live in the
portal's schema, and ``lead-monitor introspect`` prints a query built from that
schema — so a redeploy that renames a field is a settings change plus one
command, not an afternoon in DevTools.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..auth import PortalSession, authenticate
from ..config import Settings
from ..models import Lead
from ..retry import with_retries
from .base import iter_records, records_to_leads

logger = logging.getLogger(__name__)

RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)

# The real schema, established by probing the live endpoint on 2026-07-29.
# Server-side introspection is disabled, so this was recovered from the
# validator's own "Did you mean" suggestions rather than from __schema.
#
# Two things about it are easy to get wrong. Field names are PascalCase except
# for `id`, which is not — a mixture no one would guess. And there is no single
# name field: the schema carries FirstName and LastName separately, which the
# mapping layer joins back together.
DEFAULT_LEADS_QUERY = """
query Leads {
  leads {
    id
    FirstName
    LastName
    Email
    Phone
    Status
    CreatedDate
    LastActivityDate
    LastModifiedDate
  }
}
""".strip()


class GraphQLError(RuntimeError):
    """The endpoint answered 200 but the query did not succeed."""


class GraphQLLeadsClient:
    """Reads the leads collection from the portal's GraphQL endpoint."""

    def __init__(self, settings: Settings, session: PortalSession | None = None) -> None:
        self._settings = settings
        self._session = session or authenticate(settings)
        self._client = self._build_client()

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._settings.request_timeout_seconds,
            follow_redirects=True,
            cookies=self._session.cookies,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self._session.headers(),
            },
        )

    # -- public API --------------------------------------------------------

    def fetch_leads(self) -> list[Lead]:
        payload = with_retries(
            lambda: self.execute(self._query(), self._variables(), self._operation_name()),
            attempts=self._settings.max_attempts,
            retry_on=RETRYABLE,
            description="fetch leads",
        )
        records = list(iter_records(payload))
        leads = records_to_leads(records)
        logger.info("Fetched leads", extra={"count": len(leads)})
        return leads

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> Any:
        """Run one GraphQL operation and return its ``data``.

        Raises :class:`GraphQLError` when the response carries ``errors``,
        which HTTP status alone would not reveal.
        """
        body = {
            "operationName": operation_name,
            "variables": variables or {},
            "query": query,
        }

        response = self._client.post(self._settings.leads_url(), json=body)

        if response.status_code in (401, 403):
            logger.info("Session rejected; re-authenticating")
            self._session = authenticate(self._settings, force=True)
            self._client.close()
            self._client = self._build_client()
            response = self._client.post(self._settings.leads_url(), json=body)

        response.raise_for_status()

        try:
            envelope = response.json()
        except ValueError as error:
            raise GraphQLError(
                f"{self._settings.leads_url()} did not return JSON. The endpoint may have moved."
            ) from error

        errors = envelope.get("errors")
        if errors:
            # Surfaced rather than swallowed. Treating this as an empty result
            # is how a monitor goes quiet without anyone noticing.
            messages = "; ".join(
                str(item.get("message", item)) for item in errors if isinstance(item, dict)
            )
            raise GraphQLError(
                f"GraphQL query failed (HTTP {response.status_code}): {messages or errors}"
            )

        data = envelope.get("data")
        if data is None:
            raise GraphQLError("GraphQL response contained neither data nor errors")

        return data

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GraphQLLeadsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- configuration -----------------------------------------------------

    def _query(self) -> str:
        configured = self._settings.leads_graphql_query.strip()
        if configured:
            return configured
        # No warning: unlike the earlier guess, this query matches the schema
        # as probed against the live endpoint.
        return DEFAULT_LEADS_QUERY

    def _variables(self) -> dict[str, Any]:
        raw = self._settings.leads_graphql_variables.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"LEADS_GRAPHQL_VARIABLES is not valid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("LEADS_GRAPHQL_VARIABLES must be a JSON object")
        return parsed

    def _operation_name(self) -> str | None:
        name = self._settings.leads_graphql_operation.strip()
        return name or None
