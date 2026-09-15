"""The single lead representation shared by every layer.

Both the API client and the Playwright fallback produce ``Lead`` objects, so
storage, diffing and notification never learn which one ran.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Length of a Spanish national subscriber number. Comparing on the trailing
# digits makes "+34 600 123 456", "0034600123456" and "600123456" agree.
_NATIONAL_NUMBER_DIGITS = 9


def _normalise_phone(value: str) -> str:
    """Reduce a phone number to a form that compares reliably.

    Staff enter numbers with spaces, dashes, a ``+34`` prefix, a ``0034`` prefix
    or none at all, and the same person written two ways must not read as two
    leads. Punctuation is dropped and the trailing national digits are kept,
    which collapses every prefix spelling onto one value.

    The trailing-digits rule can in principle collide across countries. That is
    acceptable: the fingerprint also carries name and email, so a collision needs
    two different people to share all three.
    """
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-_NATIONAL_NUMBER_DIGITS:] if len(digits) > _NATIONAL_NUMBER_DIGITS else digits


class Lead(BaseModel):
    """A single lead as seen on the portal's Leads page."""

    model_config = ConfigDict(frozen=True)

    external_id: str = Field(min_length=1, description="The portal's own identifier for the lead.")
    name: str = ""
    email: str = ""
    phone: str = ""
    status: str = ""
    club: str = ""
    created_at: datetime | None = None
    raw: dict[str, object] = Field(default_factory=dict, repr=False)

    @field_validator("name", "email", "phone", "status", "club", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> str:
        """Collapse ``None`` and stray whitespace so identical leads compare equal."""
        if value is None:
            return ""
        return " ".join(str(value).split())

    @property
    def fingerprint(self) -> str:
        """Stable hash of the fields a human would use to identify the person.

        A secondary guard behind ``external_id``: if the portal ever renumbers
        its leads, or the same person is entered twice, the fingerprint still
        matches and no second email goes out.

        Deliberately excludes ``external_id`` — including it would make the hash
        differ exactly in the case this is meant to catch. It also excludes
        ``status``, since a lead moving from Pre Order to something else is the
        same person, not a new one.

        When the portal gives us no identifying detail at all we fall back to
        ``external_id``, because hashing three empty strings would make every
        anonymous lead collide with every other and silence real notifications.
        """
        parts = [self.name.lower(), self.email.lower(), _normalise_phone(self.phone)]
        material = "|".join(parts) if any(parts) else f"id:{self.external_id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def summary(self) -> str:
        """One-line description for logs and email subjects."""
        who = self.name or self.email or self.phone or self.external_id
        return f"{who} ({self.status or 'unknown status'})"


class RunOutcome(BaseModel):
    """What a single monitor run did, for logging and the CI summary."""

    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fetched: int = 0
    matched_filter: int = 0
    newly_recorded: int = 0
    recency_excluded: int = 0
    notified: int = 0
    seeded: bool = False
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors
