"""Leads sources. Which one runs is a configuration choice, not a code change."""

from __future__ import annotations

from ..config import LeadsClientKind, Settings
from .base import LeadsClient, matches_status, record_to_lead, records_to_leads

__all__ = [
    "LeadsClient",
    "build_client",
    "matches_status",
    "record_to_lead",
    "records_to_leads",
]


def build_client(settings: Settings) -> LeadsClient:
    """Instantiate the client the configuration asks for."""
    if settings.leads_client is LeadsClientKind.DOM:
        from .dom import DomLeadsClient

        return DomLeadsClient(settings)

    if settings.leads_client is LeadsClientKind.API:
        from .api import ApiLeadsClient

        return ApiLeadsClient(settings)

    from .graphql import GraphQLLeadsClient

    return GraphQLLeadsClient(settings)
