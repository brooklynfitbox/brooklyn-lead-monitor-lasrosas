"""Fetch leads over plain HTTP using the session Playwright captured.

This is the preferred client: one request, no browser, a few hundred
milliseconds. It is used whenever discovery found a JSON endpoint.

A 401 is treated specially. Sessions expire, and the correct response is to log
in again once and retry — not to fail the run, and not to retry blindly, which
would just spend four attempts hitting the same closed door.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..auth import PortalSession, authenticate
from ..config import Settings
from ..models import Lead
from ..retry import with_retries
from .base import iter_records, records_to_leads

logger = logging.getLogger(__name__)

# Errors worth retrying: the network glitched or the server is briefly unwell.
RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class ApiLeadsClient:
    """Reads the leads collection from the portal's internal JSON endpoint."""

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
                "X-Requested-With": "XMLHttpRequest",
                **self._session.headers(),
            },
        )

    def fetch_leads(self) -> list[Lead]:
        payload = with_retries(
            self._get_payload,
            attempts=self._settings.max_attempts,
            retry_on=RETRYABLE,
            description="fetch leads",
        )
        records = list(iter_records(payload))
        leads = records_to_leads(records)
        logger.info("Fetched leads", extra={"count": len(leads)})
        return leads

    def _get_payload(self) -> Any:
        response = self._client.get(self._settings.leads_url())

        if response.status_code in (401, 403):
            # The session died. Re-authenticate once, then let the retry
            # wrapper have another go with the fresh credentials.
            logger.info("Session rejected; re-authenticating")
            self._session = authenticate(self._settings, force=True)
            self._client.close()
            self._client = self._build_client()
            response = self._client.get(self._settings.leads_url())

        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise ValueError(
                f"Expected JSON from {self._settings.leads_url()} but got {content_type!r}. "
                "The endpoint may have moved; rerun `lead-monitor discover`."
            )

        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiLeadsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
