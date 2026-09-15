"""Fallback: read the leads out of the rendered page.

Used only when discovery finds no JSON endpoint. It is slower, heavier and more
fragile than the API client — a redesign of the table breaks it — so it exists
as insurance rather than a plan.

Column meaning is derived from the table's own header text rather than from
positional indexes, so inserting a column upstream does not silently shift every
field by one.
"""

from __future__ import annotations

import logging
from typing import Any

from ..auth import _await_login_result, _capture_failure, _fill_login_form
from ..config import Settings
from ..models import Lead
from .base import parse_datetime

logger = logging.getLogger(__name__)

# Header text (lowercased, accent-insensitive enough for these) to logical field.
_HEADER_MAP: dict[str, tuple[str, ...]] = {
    "name": ("name", "nombre", "cliente", "customer", "lead"),
    "email": ("email", "e-mail", "correo", "mail"),
    "phone": ("phone", "telefono", "teléfono", "movil", "móvil", "tel"),
    "status": ("status", "estado", "situacion", "situación"),
    "club": ("club", "centro", "center", "studio", "gimnasio"),
    "created_at": ("created", "fecha", "alta", "date", "registro"),
}


class DomLeadsClient:
    """Renders the Leads page and parses the table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_leads(self) -> list[Lead]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self._settings.headless,
                channel=self._settings.browser_channel or None,
            )
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(self._settings.login_url(), wait_until="domcontentloaded")
                _fill_login_form(page, self._settings)
                _await_login_result(page, self._settings)

                page.goto(self._settings.leads_page_url(), wait_until="networkidle")
                page.wait_for_selector("table", timeout=20_000)

                leads = parse_table(page.evaluate(_EXTRACT_TABLE_JS))
                logger.info("Parsed leads from the page", extra={"count": len(leads)})
                return leads
            except Exception:
                _capture_failure(page, self._settings, "dom-fetch-failed")
                raise
            finally:
                context.close()
                browser.close()

    def close(self) -> None:
        """Nothing is held open between calls; each fetch owns its browser."""


# Extracts the first table as {headers: [...], rows: [[...]]}. Kept as a string
# so the parsing logic below can be unit-tested without a browser.
_EXTRACT_TABLE_JS = """
() => {
  const table = document.querySelector('table');
  if (!table) return { headers: [], rows: [] };
  const text = (el) => (el.innerText || el.textContent || '').trim();
  const headerCells = table.querySelectorAll('thead th, thead td, tr:first-child th');
  const headers = Array.from(headerCells).map(text);
  const bodyRows = table.querySelectorAll('tbody tr');
  const rows = Array.from(bodyRows.length ? bodyRows : table.querySelectorAll('tr'))
    .map((tr) => Array.from(tr.querySelectorAll('td')).map(text))
    .filter((cells) => cells.length > 0);
  return { headers, rows };
}
"""


def _classify(header: str) -> str | None:
    """Map a header cell onto a logical field name."""
    lowered = header.strip().lower()
    for field, hints in _HEADER_MAP.items():
        if any(hint in lowered for hint in hints):
            return field
    return None


def parse_table(table: dict[str, Any]) -> list[Lead]:
    """Turn the extracted table into leads, keyed by header meaning."""
    headers: list[str] = table.get("headers", [])
    rows: list[list[str]] = table.get("rows", [])

    if not headers or not rows:
        logger.warning("Leads table was empty or had no headers")
        return []

    columns = {index: _classify(header) for index, header in enumerate(headers)}
    if not any(columns.values()):
        logger.warning("No recognisable columns", extra={"headers": headers})
        return []

    leads: list[Lead] = []
    for position, row in enumerate(rows):
        values: dict[str, str] = {}
        for index, cell in enumerate(row):
            field = columns.get(index)
            if field:
                values[field] = cell

        # The rendered table rarely exposes the database id, so identity comes
        # from the contact details. That is why Lead.fingerprint exists.
        identifier = values.get("email") or values.get("phone") or f"row-{position}"

        leads.append(
            Lead(
                external_id=identifier,
                name=values.get("name", ""),
                email=values.get("email", ""),
                phone=values.get("phone", ""),
                status=values.get("status", ""),
                club=values.get("club", ""),
                created_at=parse_datetime(values.get("created_at")),
                raw=dict(zip(headers, row, strict=False)),
            )
        )

    return leads
