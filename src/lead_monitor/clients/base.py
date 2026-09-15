"""The contract every leads source honours, and the mapping onto :class:`Lead`.

The portal's field names are not known until discovery runs, and they may be in
English or Spanish, camelCase or snake_case. Rather than hardcode one guess,
each logical field has a list of candidate keys and the first one present wins.
That way a schema surprise is handled by adding a candidate here instead of
rewriting the client.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from ..models import Lead

logger = logging.getLogger(__name__)

# Ordered by preference: the first key present in a record is used.
FIELD_CANDIDATES: Mapping[str, Sequence[str]] = {
    "external_id": ("id", "leadId", "lead_id", "uuid", "_id", "codigo", "code"),
    "name": ("name", "fullName", "full_name", "nombre", "nombreCompleto", "customerName"),
    "email": ("email", "mail", "correo", "correoElectronico", "emailAddress"),
    "phone": ("phone", "telephone", "mobile", "telefono", "movil", "phoneNumber"),
    "status": ("status", "state", "estado", "situacion", "leadStatus"),
    "club": ("club", "centro", "center", "centre", "studio", "gym", "clubName"),
    "created_at": (
        "CreatedDate",
        "createdAt",
        "created_at",
        "created",
        "fecha",
        "fechaAlta",
        "fecha_alta",
        "date",
        "registeredAt",
    ),
}

# The portal's schema has no single name field; it carries these two.
_GIVEN_NAME_KEYS = ("FirstName", "firstName", "first_name", "nombre")
_FAMILY_NAME_KEYS = ("LastName", "lastName", "last_name", "apellidos", "apellido")


class LeadsClient(Protocol):
    """Anything that can produce the current list of leads."""

    def fetch_leads(self) -> list[Lead]:
        """Return every lead currently visible, unfiltered by status."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...


def _first_present(record: Mapping[str, Any], keys: Sequence[str]) -> Any | None:
    """Return the first non-empty value among ``keys``, case-insensitively."""
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort timestamp parsing across the formats portals emit."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        # Milliseconds if the number is far too large to be seconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value).strip()
    # ISO 8601 with a Z suffix is the common case and fromisoformat wants +00:00.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue

    logger.debug("Unparsed timestamp", extra={"value": text})
    return None


def _full_name(record: Mapping[str, Any]) -> str:
    """Produce a display name from whichever name fields the record carries.

    The portal splits names into FirstName and LastName, so a single-field
    lookup would silently return an empty name and make every notification
    read "(no name)".
    """
    single = _first_present(record, FIELD_CANDIDATES["name"])
    if single:
        return str(single)

    given = _first_present(record, _GIVEN_NAME_KEYS) or ""
    family = _first_present(record, _FAMILY_NAME_KEYS) or ""
    return " ".join(part for part in (str(given).strip(), str(family).strip()) if part)


def record_to_lead(record: Mapping[str, Any]) -> Lead | None:
    """Map one raw record onto a :class:`Lead`.

    Returns ``None`` when the record carries no usable identifier. A lead we
    cannot identify cannot be deduplicated, and emitting it would mean an email
    on every single run forever — far worse than skipping it and logging.
    """
    external_id = _first_present(record, FIELD_CANDIDATES["external_id"])
    if external_id is None:
        logger.warning(
            "Skipping a record with no identifier",
            extra={"available_keys": sorted(str(k) for k in record)},
        )
        return None

    return Lead(
        external_id=str(external_id),
        name=_full_name(record),
        email=_first_present(record, FIELD_CANDIDATES["email"]) or "",
        phone=_first_present(record, FIELD_CANDIDATES["phone"]) or "",
        status=_first_present(record, FIELD_CANDIDATES["status"]) or "",
        club=_first_present(record, FIELD_CANDIDATES["club"]) or "",
        created_at=parse_datetime(_first_present(record, FIELD_CANDIDATES["created_at"])),
        raw=dict(record),
    )


def records_to_leads(records: Sequence[Mapping[str, Any]]) -> list[Lead]:
    """Map a batch, dropping unusable records rather than failing the run."""
    leads = [lead for lead in (record_to_lead(r) for r in records) if lead is not None]
    if len(leads) != len(records):
        logger.warning(
            "Some records could not be mapped",
            extra={"received": len(records), "mapped": len(leads)},
        )
    return leads


def matches_status(lead: Lead, wanted: str) -> bool:
    """Decide whether a lead is one we care about.

    An empty ``wanted`` means every lead qualifies, and that is the default.
    The reason is a race that filtering would lose to: in this portal a lead
    sits in 'Pre Order' only until a member of staff opens it, at which point
    they move it to 'Nurturing'. Polling every ten minutes, a lead created at
    10:02 and picked up at 10:07 is already out of Pre Order by the time the
    10:10 run looks — so a status filter would silently never mention it, and
    would fail hardest exactly when the team is responding quickly.

    Filtering on 'is this lead new to us', which the database can answer with
    certainty, has no such window. The status is still captured and shown in
    the notification, so a lead that arrived unhandled is obvious.

    Set LEADS_STATUS_FILTER to restore filtering if that trade is not wanted.
    Comparison is forgiving about case, spacing and punctuation: 'Pre Order',
    'pre-order' and 'PREORDER' are the same thing.
    """
    if not wanted.strip():
        return True

    def canonical(text: str) -> str:
        return "".join(ch for ch in text.lower() if ch.isalnum())

    return canonical(lead.status) == canonical(wanted)


def iter_records(payload: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Yield record dictionaries from whatever envelope the API used."""
    if depth > 6:
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        preferred = ("data", "results", "items", "leads", "records", "rows", "content", "edges")
        for key in preferred:
            if key in payload:
                yield from iter_records(payload[key], depth + 1)
                return
        for value in payload.values():
            if isinstance(value, list | dict):
                yield from iter_records(value, depth + 1)
                return
