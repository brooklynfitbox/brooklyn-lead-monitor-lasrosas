"""SQLite persistence for seen leads.

The whole point of this module is that a lead is never announced twice. That is
harder than it looks, because a run can die at any instant — the runner can be
evicted, the SMTP server can hang, the process can be cancelled mid-send. So the
write is deliberately split into two phases:

1. ``record_seen`` inserts the lead with ``notified_at`` NULL. Insertion is
   ``INSERT OR IGNORE`` against a primary key, so a lead already known is a
   no-op no matter how many times it is offered.
2. ``mark_notified`` stamps ``notified_at`` only *after* the email server has
   accepted the message.

The gap between the two is the only window in which a crash can cause a repeat,
and a repeat is the correct direction to fail: a duplicate email is an
annoyance, a silently dropped lead is lost revenue. Everything else — a crash
before the insert, between the insert and the send, or during the send — results
in the lead being retried on the next run.

Note there is no ``UPDATE`` of lead fields on re-observation. A lead whose status
later changes is not new, and rewriting the row would serve no purpose while
risking a fingerprint change that looks like a fresh lead.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from .models import Lead, RunOutcome

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    external_id   TEXT    PRIMARY KEY,
    fingerprint   TEXT    NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL DEFAULT '',
    phone         TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT '',
    club          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT,
    first_seen_at TEXT    NOT NULL,
    notified_at   TEXT,
    notify_failures INTEGER NOT NULL DEFAULT 0,
    raw           TEXT    NOT NULL DEFAULT '{}'
);

-- Secondary identity guard, see Lead.fingerprint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_fingerprint ON leads (fingerprint);

-- The pending-notification query runs every ten minutes; keep it cheap.
CREATE INDEX IF NOT EXISTS idx_leads_pending ON leads (notified_at)
    WHERE notified_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT    NOT NULL,
    fetched      INTEGER NOT NULL DEFAULT 0,
    newly_recorded INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 1,
    errors       TEXT    NOT NULL DEFAULT ''
);
"""


class LeadStore:
    """A connection to the leads database. Use as a context manager.

    ``store_personal_data`` controls whether ``record_seen`` writes name,
    email, phone and the raw API payload into the row, or leaves them blank.
    This exists because the database is committed to a git branch — unlike
    the notification email, which is redacted by default already (see
    notifier.py), storage had no equivalent guard until this flag, so a
    private repo's history quietly held full contact details regardless of
    the email settings. Production wires this to
    ``Settings.notify_include_personal_data``, so the two stay in lockstep:
    if the email doesn't carry a name, neither does the database. Defaults to
    ``True`` so direct construction (mainly tests, and read-only CLI
    commands that never call ``record_seen``) is unaffected.
    """

    def __init__(self, path: Path, *, store_personal_data: bool = True) -> None:
        self.path = path
        self._store_personal_data = store_personal_data
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> LeadStore:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # The database file is committed to a git branch between runs, so keep
        # it to a single file: WAL would leave -wal/-shm siblings behind.
        conn.execute("PRAGMA journal_mode = DELETE")
        # Durability beats speed here. A run writes a handful of rows every ten
        # minutes; losing them to a lost page cache would mean duplicate emails.
        conn.execute("PRAGMA synchronous = FULL")
        self._conn = conn
        self._migrate()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("LeadStore is not connected; call connect() first")
        return self._conn

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            logger.info("Initialised database", extra={"path": str(self.path)})
        elif row["version"] > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database at {self.path} is version {row['version']}, "
                f"newer than this code understands ({SCHEMA_VERSION}). Refusing to run."
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- queries -----------------------------------------------------------

    def is_empty(self) -> bool:
        """True when no lead has ever been recorded.

        Drives the cold-start seed: on a brand new database every lead on the
        portal looks new, and mailing all of them at once is never what anyone
        wants.
        """
        row = self.conn.execute("SELECT 1 FROM leads LIMIT 1").fetchone()
        return row is None

    def known_ids(self) -> set[str]:
        return {r["external_id"] for r in self.conn.execute("SELECT external_id FROM leads")}

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()
        return int(row["n"])

    # -- phase one: record -------------------------------------------------

    def record_seen(self, leads: Iterable[Lead], *, already_notified: bool = False) -> list[Lead]:
        """Insert leads that are not yet known and return only those inserted.

        ``already_notified`` stamps ``notified_at`` at insert time. It exists for
        the cold-start seed, where the intent is precisely to record leads
        *without* announcing them.

        The returned leads carry the full in-memory data the caller passed in
        (for logging/counting purposes) even when ``store_personal_data`` is
        off — it's what's written to the row that's redacted, not this
        return value. Nothing downstream sends this return value anywhere;
        only its length is used.
        """
        inserted: list[Lead] = []
        now = datetime.now(UTC).isoformat()

        with self._transaction() as conn:
            for lead in leads:
                name = lead.name if self._store_personal_data else ""
                email = lead.email if self._store_personal_data else ""
                phone = lead.phone if self._store_personal_data else ""
                raw = lead.raw if self._store_personal_data else {}
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO leads (
                        external_id, fingerprint, name, email, phone, status, club,
                        created_at, first_seen_at, notified_at, raw
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lead.external_id,
                        lead.fingerprint,
                        name,
                        email,
                        phone,
                        lead.status,
                        lead.club,
                        lead.created_at.isoformat() if lead.created_at else None,
                        now,
                        now if already_notified else None,
                        json.dumps(raw, ensure_ascii=False, default=str),
                    ),
                )
                if cursor.rowcount:
                    inserted.append(lead)

        if inserted:
            logger.info(
                "Recorded new leads",
                extra={"count": len(inserted), "seeded": already_notified},
            )
        return inserted

    # -- phase two: notify -------------------------------------------------

    def pending_notifications(self) -> list[Lead]:
        """Leads recorded but not yet successfully emailed about.

        Includes leads from earlier runs that failed to send, which is what
        makes the delivery guarantee hold across a crash.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM leads
            WHERE notified_at IS NULL
            ORDER BY first_seen_at ASC, external_id ASC
            """
        ).fetchall()
        return [self._row_to_lead(row) for row in rows]

    def mark_notified(self, external_ids: Iterable[str]) -> int:
        """Stamp leads as notified. Called only after the mail server accepts."""
        ids = list(external_ids)
        if not ids:
            return 0

        now = datetime.now(UTC).isoformat()
        with self._transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE leads SET notified_at = ?
                WHERE external_id IN ({",".join("?" * len(ids))})
                  AND notified_at IS NULL
                """,
                (now, *ids),
            )
        logger.info("Marked leads notified", extra={"count": cursor.rowcount})
        return int(cursor.rowcount)

    def record_notify_failure(self, external_ids: Iterable[str]) -> None:
        """Count a failed delivery attempt, for visibility into stuck leads."""
        ids = list(external_ids)
        if not ids:
            return
        with self._transaction() as conn:
            conn.execute(
                f"""
                UPDATE leads SET notify_failures = notify_failures + 1
                WHERE external_id IN ({",".join("?" * len(ids))})
                """,
                tuple(ids),
            )

    # -- observability -----------------------------------------------------

    def record_run(self, outcome: RunOutcome) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    started_at, finished_at, fetched, newly_recorded, notified, ok, errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.started_at.isoformat(),
                    datetime.now(UTC).isoformat(),
                    outcome.fetched,
                    outcome.newly_recorded,
                    outcome.notified,
                    1 if outcome.ok else 0,
                    " | ".join(outcome.errors),
                ),
            )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_lead(row: sqlite3.Row) -> Lead:
        created = row["created_at"]
        raw = row["raw"]
        return Lead(
            external_id=row["external_id"],
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            status=row["status"],
            club=row["club"],
            created_at=datetime.fromisoformat(created) if created else None,
            raw=json.loads(raw) if raw else {},
        )
