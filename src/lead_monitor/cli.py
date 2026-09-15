"""Command line entry points.

Each command exists to be run on its own, because the alternative — a single
``run`` that does everything — makes diagnosing a broken deployment a matter of
guesswork. ``test-notify`` proves the notification channel's credentials
independently of the portal, ``discover`` proves the portal independently of
the notifier, and ``status`` answers "is it actually working" without reading
a log.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import NotifyChannel, Settings, load_settings
from .logging_setup import configure_logging
from .models import Lead
from .store import LeadStore

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2


def _bootstrap() -> Settings:
    """Load settings and start logging, or explain what is missing and stop."""
    try:
        settings = load_settings()
    except Exception as error:
        # Logging is not configured yet, and this must be readable in a CI log.
        print(f"Configuration error:\n{error}", file=sys.stderr)
        raise SystemExit(EXIT_MISCONFIGURED) from error

    configure_logging(
        settings.log_level,
        plain=settings.log_plain,
        secrets=settings.secret_values(),
    )
    return settings


def cmd_run(_: argparse.Namespace) -> int:
    """One monitoring pass. This is what the schedule calls."""
    from .monitor import run_once

    settings = _bootstrap()
    outcome = run_once(settings)

    logger.info(
        "Run complete",
        extra={
            "fetched": outcome.fetched,
            "matching": outcome.matched_filter,
            "new": outcome.newly_recorded,
            "recency_excluded": outcome.recency_excluded,
            "notified": outcome.notified,
            "seeded": outcome.seeded,
            "ok": outcome.ok,
        },
    )
    return EXIT_OK if outcome.ok else EXIT_FAILED


def cmd_discover(args: argparse.Namespace) -> int:
    """Work out how the Leads page fetches its data."""
    from .discovery import run_discovery

    settings = _bootstrap()
    report = run_discovery(settings, Path(args.output))

    print(f"\nReport written to {report}\n")
    print(report.read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_introspect(_: argparse.Namespace) -> int:
    """Ask the GraphQL schema what the leads query is."""
    from .introspect import run_introspection

    settings = _bootstrap()
    try:
        report = run_introspection(settings)
    except Exception as error:
        logger.exception("Introspection failed")
        print(f"\nFailed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED

    print(report)
    return EXIT_OK


def cmd_test_notify(_: argparse.Namespace) -> int:
    """Send one test notification per configured channel, so each channel's
    credentials can be verified independently of the others and of the
    portal. Respects NOTIFY_CHANNEL — email, whatsapp, or both at once."""
    from .notifier import build_channel_notifier

    settings = _bootstrap()
    sample = Lead(
        external_id="test-0000",
        name="Test Lead",
        email="test@example.com",
        phone="600000000",
        status=settings.leads_status_filter,
        club="Configuration check",
    )

    any_failed = False
    for channel in settings.notify_channels:
        if channel is NotifyChannel.EMAIL:
            destination = ", ".join(settings.recipients)
        elif channel is NotifyChannel.WHATSAPP:
            destination = ", ".join(settings.whatsapp_recipients)
        else:
            destination = settings.callmebot_phone
        try:
            build_channel_notifier(settings, channel).send([sample])
        except Exception as error:
            logger.exception("Test notification failed", extra={"channel": channel.value})
            print(f"\n{channel.value}: FAILED — {type(error).__name__}: {error}", file=sys.stderr)
            any_failed = True
        else:
            print(f"\n{channel.value}: sent to {destination}")

    return EXIT_FAILED if any_failed else EXIT_OK


def cmd_status(_: argparse.Namespace) -> int:
    """Summarise the database: how many leads, how many stuck, when last seen."""
    settings = _bootstrap()

    if not settings.database_path.exists():
        print(f"No database at {settings.database_path} — nothing has run yet.")
        return EXIT_OK

    with LeadStore(
        settings.database_path, store_personal_data=settings.notify_include_personal_data
    ) as store:
        total = store.count()
        pending = store.pending_notifications()
        last = store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        stuck = store.conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE notified_at IS NULL AND notify_failures > 0"
        ).fetchone()["n"]

    print(f"Leads recorded:      {total}")
    print(f"Awaiting notification: {len(pending)}")
    print(f"Failed at least once:  {stuck}")
    if last is not None:
        print(
            f"Last run:            {last['finished_at']} "
            f"({'ok' if last['ok'] else 'FAILED'}), "
            f"{last['fetched']} fetched, {last['notified']} notified"
        )
    return EXIT_OK


def cmd_init_db(_: argparse.Namespace) -> int:
    """Create the database file without fetching anything."""
    settings = _bootstrap()
    with LeadStore(
        settings.database_path, store_personal_data=settings.notify_include_personal_data
    ) as store:
        print(f"Database ready at {settings.database_path} ({store.count()} leads)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lead-monitor",
        description="Watch the Brooklyn Fitboxing portal for new Pre Order leads.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("run", help="perform one monitoring pass").set_defaults(func=cmd_run)

    discover = commands.add_parser("discover", help="find the leads endpoint")
    discover.add_argument("--output", default="discovery", help="where to write the report")
    discover.set_defaults(func=cmd_discover)

    commands.add_parser(
        "introspect", help="ask the GraphQL schema for the leads query"
    ).set_defaults(func=cmd_introspect)
    commands.add_parser("test-notify", help="send one test notification").set_defaults(
        func=cmd_test_notify
    )
    commands.add_parser("status", help="summarise the database").set_defaults(func=cmd_status)
    commands.add_parser("init-db", help="create the database file").set_defaults(func=cmd_init_db)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
