"""Tests for session extraction and caching.

The browser login itself is not unit-tested — it needs a real portal, and a
mocked Playwright would only assert that the mock was called. What is tested is
everything around it: reading a storage state, deciding whether a cached session
is still good, and refusing one that is useless.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lead_monitor.auth import (
    PortalSession,
    load_cached_session,
    session_from_storage_state,
)
from lead_monitor.config import Settings


def make_settings(tmp: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://portal.example.com",
        "portal_username": "user",
        "portal_password": "portal-secret",
        "smtp_host": "smtp.example.com",
        "smtp_username": "mailer@example.com",
        "smtp_password": "smtp-secret",
        "mail_from": "mailer@example.com",
        "mail_to": "ops@example.com",
        "storage_state_path": tmp / "storage_state.json",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


COOKIE_STATE = {
    "cookies": [
        {"name": "sessionid", "value": "abc123", "domain": "portal.example.com"},
        {"name": "csrftoken", "value": "xyz789", "domain": "portal.example.com"},
    ],
    "origins": [],
}

TOKEN_STATE = {
    "cookies": [],
    "origins": [
        {
            "origin": "https://portal.example.com",
            "localStorage": [
                {"name": "theme", "value": "dark"},
                {"name": "access_token", "value": "eyJhbGciOi.payload.sig"},
            ],
        }
    ],
}


class TestExtraction(unittest.TestCase):
    def test_reads_cookies(self) -> None:
        session = session_from_storage_state(COOKIE_STATE)
        self.assertEqual(session.cookies["sessionid"], "abc123")
        self.assertTrue(session.is_usable)

    def test_reads_a_bearer_token_from_local_storage(self) -> None:
        session = session_from_storage_state(TOKEN_STATE)
        self.assertEqual(session.bearer_token, "eyJhbGciOi.payload.sig")
        self.assertEqual(session.headers()["Authorization"], "Bearer eyJhbGciOi.payload.sig")

    def test_ignores_unrelated_local_storage_keys(self) -> None:
        state = {
            "cookies": [],
            "origins": [
                {"localStorage": [{"name": "theme", "value": "dark"}]},
            ],
        }
        self.assertIsNone(session_from_storage_state(state).bearer_token)

    def test_unwraps_a_token_stored_as_json(self) -> None:
        """Some apps keep {"accessToken": "..."} rather than the bare string."""
        state = {
            "cookies": [],
            "origins": [
                {
                    "localStorage": [
                        {"name": "auth", "value": json.dumps({"accessToken": "nested-token"})}
                    ]
                }
            ],
        }
        self.assertEqual(session_from_storage_state(state).bearer_token, "nested-token")

    def test_survives_malformed_json_in_local_storage(self) -> None:
        state = {
            "cookies": [],
            "origins": [{"localStorage": [{"name": "token", "value": "{not json"}]}],
        }
        self.assertIsNone(session_from_storage_state(state).bearer_token)

    def test_an_empty_state_is_not_usable(self) -> None:
        session = session_from_storage_state({"cookies": [], "origins": []})
        self.assertFalse(session.is_usable)

    def test_collects_cookies_and_token_together(self) -> None:
        """Which one the portal wants is unknown until discovery; carry both."""
        combined = {"cookies": COOKIE_STATE["cookies"], "origins": TOKEN_STATE["origins"]}
        session = session_from_storage_state(combined)
        self.assertTrue(session.cookies)
        self.assertTrue(session.bearer_token)


class TestFreshness(unittest.TestCase):
    def test_a_new_session_is_fresh(self) -> None:
        self.assertTrue(PortalSession(cookies={"a": "b"}).is_fresh(120))

    def test_an_old_session_is_not(self) -> None:
        old = PortalSession(
            cookies={"a": "b"},
            captured_at=datetime.now(UTC) - timedelta(minutes=180),
        )
        self.assertFalse(old.is_fresh(120))


class TestCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_cache_returns_nothing(self) -> None:
        self.assertIsNone(load_cached_session(make_settings(self.tmp)))

    def test_reads_a_fresh_cache(self) -> None:
        settings = make_settings(self.tmp)
        payload = dict(COOKIE_STATE)
        payload["_captured_at"] = datetime.now(UTC).isoformat()
        settings.storage_state_path.write_text(json.dumps(payload), encoding="utf-8")

        session = load_cached_session(settings)
        assert session is not None
        self.assertEqual(session.cookies["sessionid"], "abc123")

    def test_rejects_an_expired_cache(self) -> None:
        settings = make_settings(self.tmp, storage_state_max_age_minutes=60)
        payload = dict(COOKIE_STATE)
        payload["_captured_at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        settings.storage_state_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(load_cached_session(settings))

    def test_ignores_a_corrupt_cache_instead_of_crashing(self) -> None:
        """A truncated file must trigger a fresh login, not take the run down."""
        settings = make_settings(self.tmp)
        settings.storage_state_path.write_text("{ truncated", encoding="utf-8")

        self.assertIsNone(load_cached_session(settings))

    def test_ignores_a_cache_with_nothing_useful_in_it(self) -> None:
        settings = make_settings(self.tmp)
        settings.storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        self.assertIsNone(load_cached_session(settings))


class TestCapturedAuthHeaders(unittest.TestCase):
    """The portal's real scheme: the credential is a header the app attaches
    per request, absent from cookies and storage. It can only be observed on a
    live request and then replayed."""

    def test_headers_include_the_captured_authorization(self) -> None:
        session = PortalSession(
            cookies={"sid": "x"},
            auth_headers={"Authorization": "Bearer live-token", "X-Club": "42"},
        )
        headers = session.headers()
        self.assertEqual(headers["Authorization"], "Bearer live-token")
        self.assertEqual(headers["X-Club"], "42")

    def test_captured_authorization_overrides_a_storage_token(self) -> None:
        """When both exist, the header the app actually sent is the correct one."""
        session = PortalSession(
            bearer_token="stale-storage-token",
            auth_headers={"Authorization": "Bearer real-token"},
        )
        self.assertEqual(session.headers()["Authorization"], "Bearer real-token")

    def test_auth_headers_alone_make_a_session_usable(self) -> None:
        self.assertTrue(PortalSession(auth_headers={"Authorization": "Bearer x"}).is_usable)

    def test_a_session_with_nothing_is_not_usable(self) -> None:
        self.assertFalse(PortalSession().is_usable)

    def test_captured_headers_survive_the_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from lead_monitor.auth import _cache_session

            settings = make_settings(Path(tmp))
            _cache_session(
                {"cookies": COOKIE_STATE["cookies"], "origins": []},
                settings,
                {"Authorization": "Bearer cached", "X-Club": "42"},
            )
            reloaded = load_cached_session(settings)

        assert reloaded is not None
        self.assertEqual(reloaded.auth_headers["Authorization"], "Bearer cached")
        self.assertEqual(reloaded.headers()["X-Club"], "42")


if __name__ == "__main__":
    unittest.main()
