"""The run loop: fetch, diff, notify, mark.

Deliberately small, because the interesting decisions live in the modules it
calls. What it owns is the ordering, and the ordering is the whole delivery
guarantee: nothing is stamped as notified until the mail server has said yes.

The cold-start seed is the other thing worth understanding here. On a brand new
database every lead on the portal is unseen, and mailing the entire backlog in
one go is never what anyone wants — so the first run records what exists without
announcing it, and real notifications begin from the next run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from .clients import build_client, matches_status
from .clients.base import LeadsClient
from .config import Settings
from .models import Lead, RunOutcome
from .notifier import Notifier, build_notifier
from .recency import is_recent, recency_cutoff
from .store import LeadStore

logger = logging.getLogger(__name__)


def run_once(
    settings: Settings,
    *,
    client: LeadsClient | None = None,
    notifier: Notifier | None = None,
    store: LeadStore | None = None,
) -> RunOutcome:
    """Perform one monitoring pass and return what happened.

    Every collaborator is injectable so the orchestration can be tested without
    a portal, a mail server or a real database.
    """
    outcome = RunOutcome()

    owns_store = store is None
    store = store or LeadStore(
        settings.database_path, store_personal_data=settings.notify_include_personal_data
    )
    if owns_store:
        store.connect()

    owns_client = client is None

    try:
        client = client or build_client(settings)
        notifier = notifier or build_notifier(settings)

        leads = client.fetch_leads()
        outcome.fetched = len(leads)

        wanted = [lead for lead in leads if matches_status(lead, settings.leads_status_filter)]
        outcome.matched_filter = len(wanted)
        logger.info(
            "Filtered leads",
            extra={
                "fetched": len(leads),
                "matching": len(wanted),
                "status": settings.leads_status_filter,
            },
        )

        first_run = store.is_empty()
        if first_run and settings.seed_without_notifying:
            recorded = store.record_seen(wanted, already_notified=True)
            outcome.newly_recorded = len(recorded)
            outcome.seeded = True
            logger.info(
                "Seeded the database on first run; no email sent",
                extra={"recorded": len(recorded)},
            )
        elif settings.notify_recent_leads_only:
            recent, stale = _split_by_recency(wanted, settings=settings)
            outcome.recency_excluded = len(stale)

            silenced = store.record_seen(stale, already_notified=True) if stale else []
            if silenced:
                logger.info(
                    "Recorded backlog pre-order leads without notifying",
                    extra={"count": len(silenced)},
                )

            recorded = store.record_seen(recent)
            outcome.newly_recorded = len(recorded) + len(silenced)
        else:
            recorded = store.record_seen(wanted)
            outcome.newly_recorded = len(recorded)

        # Includes anything left unsent by an earlier failed run, which is what
        # makes the guarantee survive a crash.
        pending = store.pending_notifications()
        if pending:
            outcome.notified = _notify(pending, notifier=notifier, store=store, outcome=outcome)
        else:
            logger.info("Nothing new to report")

    except Exception as error:
        logger.exception("Run failed")
        outcome.errors.append(f"{type(error).__name__}: {error}")
    finally:
        try:
            store.record_run(outcome)
        finally:
            if owns_client and client is not None:
                client.close()
            if owns_store:
                store.close()

    return outcome


def _split_by_recency(
    leads: Sequence[Lead], *, settings: Settings
) -> tuple[list[Lead], list[Lead]]:
    """Partition ``leads`` into (recent, stale) against the Clarita window.

    Recent leads are the ones worth a notification. Stale ones are still
    returned so the caller can record them as already-notified: once stored,
    the DB dedup in store.py keeps them from ever being reconsidered, so a
    lead that ages out of the window is silenced exactly once, not forever
    re-evaluated.
    """
    cutoff = recency_cutoff(datetime.now(UTC), timezone=settings.notify_timezone)
    recent: list[Lead] = []
    stale: list[Lead] = []
    for lead in leads:
        (recent if is_recent(lead.created_at, cutoff=cutoff) else stale).append(lead)
    return recent, stale


def _notify(
    pending: Sequence[Lead],
    *,
    notifier: Notifier,
    store: LeadStore,
    outcome: RunOutcome,
) -> int:
    """Send, then stamp. Never the other way round."""
    logger.info("Notifying", extra={"count": len(pending)})
    identifiers = [lead.external_id for lead in pending]

    try:
        notifier.send(pending)
    except Exception as error:
        # Leave them queued. The next run retries, and the failure counter makes
        # a permanently stuck lead visible rather than silent.
        store.record_notify_failure(identifiers)
        outcome.errors.append(f"notification failed: {type(error).__name__}: {error}")
        logger.error("Notification failed; leads stay queued", extra={"count": len(pending)})
        return 0

    return store.mark_notified(identifiers)
