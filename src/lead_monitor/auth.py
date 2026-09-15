"""Authentication against the portal, and only that.

Playwright is expensive: a browser download in CI, a few hundred megabytes of
runtime, several seconds per launch. Running one every ten minutes to read a
table would be wasteful, so its job here is narrow — perform the login once,
capture the resulting session, and get out of the way. Steady-state runs reuse
the captured session over plain HTTP and never start a browser at all.

The session is written to disk in Playwright's ``storage_state`` format and
reused until it ages out or the portal rejects it. That means a normal run is a
single HTTP request, and a browser launch happens roughly once every couple of
hours.

Login forms are located heuristically rather than by hardcoded selectors,
because the portal's markup is not known ahead of time and a class name that
changes on the next deploy should not need a code change. Explicit selectors in
the configuration override the heuristic whenever it guesses wrong.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings

if TYPE_CHECKING:  # pragma: no cover - import cost is only paid at runtime
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

# Cookie names that portals commonly use for the signed-in session.
_SESSION_COOKIE_HINTS = ("session", "sid", "auth", "token", "jwt", "connect.sid")

# localStorage / sessionStorage keys that commonly hold a bearer token.
_TOKEN_KEY_HINTS = ("token", "jwt", "access", "auth", "bearer", "id_token")


class AuthenticationError(RuntimeError):
    """Login failed, or the captured session contained nothing usable."""


@dataclass(frozen=True)
class PortalSession:
    """Whatever the portal uses to recognise an authenticated caller."""

    cookies: dict[str, str] = field(default_factory=dict)
    bearer_token: str | None = None
    # Auth headers lifted verbatim from a request the app itself made. This is
    # the portal's actual scheme: the credential lives in a JS variable in
    # memory, not in cookies or storage, and is attached explicitly to every
    # call — so it cannot be reconstructed, only observed on a live request.
    auth_headers: dict[str, str] = field(default_factory=dict)
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_usable(self) -> bool:
        return bool(self.cookies) or bool(self.bearer_token) or bool(self.auth_headers)

    def headers(self) -> dict[str, str]:
        """Headers that identify this session to the portal."""
        headers: dict[str, str] = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        # Captured headers win: they are exactly what the app sent, including
        # the correct Authorization value when the token was never in storage.
        headers.update(self.auth_headers)
        return headers

    def is_fresh(self, max_age_minutes: int) -> bool:
        return datetime.now(UTC) - self.captured_at < timedelta(minutes=max_age_minutes)


def load_cached_session(settings: Settings) -> PortalSession | None:
    """Read a previously captured session, if one exists and is still young."""
    path = settings.storage_state_path
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Ignoring unreadable session cache", extra={"error": str(error)})
        return None

    captured_raw = raw.get("_captured_at")
    try:
        captured = datetime.fromisoformat(captured_raw) if captured_raw else datetime.now(UTC)
    except ValueError:
        captured = datetime.now(UTC)

    session = session_from_storage_state(raw, captured_at=captured)
    cached_headers = raw.get("_auth_headers")
    if isinstance(cached_headers, dict) and cached_headers:
        session = replace(session, auth_headers=dict(cached_headers))
    if not session.is_usable:
        return None
    if not session.is_fresh(settings.storage_state_max_age_minutes):
        logger.info("Cached session has aged out; will re-authenticate")
        return None

    logger.info("Reusing cached portal session")
    return session


def session_from_storage_state(
    state: Mapping[str, Any],
    *,
    captured_at: datetime | None = None,
) -> PortalSession:
    """Extract cookies and any bearer token from a Playwright storage state.

    Both are collected because portals split roughly evenly between cookie
    sessions and token-in-localStorage, and which one this portal uses is not
    known until discovery runs. Sending both costs nothing and removes a whole
    class of "works locally, fails in CI" surprise.

    Typed as ``Mapping`` rather than ``dict``: this is called with both a
    plain ``dict`` (a cache file loaded from disk) and Playwright's
    ``StorageState`` TypedDict (a live browser context), and the function
    only ever reads from it. ``Mapping`` accepts either; ``dict[str, Any]``
    does not, since a TypedDict is not a structural subtype of it.
    """
    cookies = {
        cookie["name"]: cookie["value"]
        for cookie in state.get("cookies", [])
        if cookie.get("name") and cookie.get("value")
    }

    bearer = _find_bearer_token(state)

    return PortalSession(
        cookies=cookies,
        bearer_token=bearer,
        captured_at=captured_at or datetime.now(UTC),
    )


def _find_bearer_token(state: Mapping[str, Any]) -> str | None:
    """Look through localStorage for something that looks like a bearer token."""
    for origin in state.get("origins", []):
        for entry in origin.get("localStorage", []):
            name = str(entry.get("name", "")).lower()
            value = str(entry.get("value", ""))
            if not value or not any(hint in name for hint in _TOKEN_KEY_HINTS):
                continue

            # Some apps store {"accessToken": "..."} rather than the bare string.
            if value.startswith("{"):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                for key, nested in parsed.items():
                    if isinstance(nested, str) and any(
                        hint in key.lower() for hint in _TOKEN_KEY_HINTS
                    ):
                        return nested
                continue

            return value
    return None


def authenticate(settings: Settings, *, force: bool = False) -> PortalSession:
    """Return a usable session, reusing the cached one unless ``force``."""
    if not force:
        cached = load_cached_session(settings)
        if cached is not None:
            return cached
    return login_with_browser(settings)


def login_with_browser(settings: Settings) -> PortalSession:
    """Drive a real browser through the login form and capture the session."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    logger.info("Authenticating with the portal", extra={"url": settings.login_url()})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=settings.headless,
            channel=settings.browser_channel or None,
        )
        context = browser.new_context()
        page = context.new_page()

        # Record the auth headers the app attaches to its own API calls. The
        # credential is not in cookies or storage — it lives in memory and is
        # added per request — so the only way to obtain it is to watch a real
        # request leave the page.
        captured: dict[str, str] = {}
        endpoint = settings.leads_api_path

        def _remember_auth_headers(request: Any) -> None:
            if endpoint in request.url:
                for name, value in request.headers.items():
                    lname = name.lower()
                    # Skip headers httpx sets itself; keep the app's own auth.
                    if (
                        (lname == "authorization" or lname.startswith("x-"))
                        and lname not in {"x-requested-with"}
                        and value
                    ):
                        captured[name] = value

        page.on("request", _remember_auth_headers)

        try:
            page.goto(settings.login_url(), wait_until="domcontentloaded")
            _fill_login_form(page, settings)
            _await_login_result(page, settings)

            # Visit the leads page so the app fires its authenticated API call
            # and the listener above captures the headers it sends.
            page.goto(settings.leads_page_url(), wait_until="networkidle")
            page.wait_for_timeout(2_000)

            state = context.storage_state()
        except PlaywrightTimeout as error:
            _capture_failure(page, settings, "login-timeout")
            raise AuthenticationError(f"Timed out during login: {error}") from error
        except AuthenticationError:
            _capture_failure(page, settings, "login-failed")
            raise
        except Exception as error:
            _capture_failure(page, settings, "login-error")
            raise AuthenticationError(f"Login failed: {error}") from error
        finally:
            context.close()
            browser.close()

    session = session_from_storage_state(state)
    session = replace(session, auth_headers=captured)

    if not session.is_usable:
        raise AuthenticationError(
            "Login appeared to succeed but produced no cookies, token or auth "
            "headers. The portal may use a scheme this code does not recognise."
        )
    if not captured:
        # Not fatal — cookies may yet be enough — but on this portal they are
        # not, so make the likely cause visible rather than let the first
        # fetch fail with an opaque 'Not Authorised'.
        logger.warning(
            "Captured no Authorization/x-* header from the app's own request. "
            "If fetches come back unauthorised, the app may send the token on a "
            "differently named header; set it via configuration."
        )

    _cache_session(state, settings, captured)
    logger.info(
        "Authenticated",
        extra={
            "cookies": len(session.cookies),
            "bearer": bool(session.bearer_token),
            "auth_headers": sorted(captured),
        },
    )
    return session


def _fill_login_form(page: Page, settings: Settings) -> None:
    """Type the credentials, using explicit selectors when configured.

    Confirmed against the live portal: the account is pinned to the
    subdomain rather than typed in ('A-CORUNA / finisterre@...' is shown as
    a fixed block), so the login screen carries no username/email input at
    all — only a password field. A heuristic that assumed one always exists
    would either fill the wrong element or hang waiting for one that will
    never appear, so the username step is skipped whenever no such field is
    present rather than assumed mandatory.
    """
    password_field = (
        page.locator(settings.login_password_selector)
        if settings.login_password_selector
        else page.locator("input[type='password']").first
    )
    # Generous on purpose: a cold CI runner with no warmed cache can take
    # noticeably longer than a local browser to fetch and hydrate the SPA
    # bundle before this field exists at all.
    password_field.wait_for(state="visible", timeout=settings.login_timeout_seconds * 1000)

    if settings.login_username_selector:
        username_field = page.locator(settings.login_username_selector)
        username_field.fill(settings.portal_username)
    else:
        # The username input, when there is one, is essentially always the
        # visible text-like field that precedes the password field in the
        # same form.
        #
        # On this portal it depends on whether the server already recognises
        # the browser. A session with a prior visit gets the account shown as
        # a fixed block and only a password field; a browser with nothing
        # stored — which is every GitHub Actions run — gets both an email
        # field and the password field together on one screen. Both cases
        # must work, so the email field is filled when present and skipped,
        # not treated as an error, when it is not.
        #
        # input:not([type]) additionally catches component libraries' hidden
        # a11y focus targets — here, a readonly combobox behind the "Your
        # language" dropdown, which sorts before the email field in the DOM
        # and would otherwise win a plain .first. :not([readonly]) excludes
        # it at the selector level rather than after the fact.
        username_field = page.locator(
            "input[type='email']:not([readonly]), "
            "input[type='text']:not([readonly]), "
            "input:not([type]):not([readonly])"
        ).first
        # is_visible() on a locator matching nothing returns False rather
        # than raising, and short-circuits before is_editable() would need
        # to resolve a nonexistent element. is_editable() is kept as a second
        # guard in case some other portal's equivalent field is disabled
        # rather than readonly.
        if username_field.is_visible() and username_field.is_editable():
            username_field.fill(settings.portal_username)
        else:
            logger.info("No fillable username field on the login form; account is fixed by the URL")

    password_field.fill(settings.portal_password.get_secret_value())

    if settings.login_submit_selector:
        page.locator(settings.login_submit_selector).click()
    else:
        # Enter submits the form without needing to find the button, which is
        # the part of a login page that varies most between applications.
        password_field.press("Enter")


def _await_login_result(page: Page, settings: Settings) -> None:
    """Confirm the login actually worked rather than silently re-rendering."""
    timeout_ms = settings.login_timeout_seconds * 1000
    if settings.login_success_selector:
        page.locator(settings.login_success_selector).wait_for(state="visible", timeout=timeout_ms)
        return

    page.wait_for_load_state("networkidle", timeout=timeout_ms)

    # Without a success selector the best available signal is having left the
    # login page. A form that re-renders itself is the classic "wrong password"
    # response, and treating it as success would mean silently monitoring
    # nothing.
    if settings.portal_login_path.rstrip("/") in page.url:
        raise AuthenticationError(
            f"Still on the login page after submitting ({page.url}). "
            "Credentials are probably wrong, or the form needs LOGIN_SUCCESS_SELECTOR set."
        )


def _cache_session(
    state: Mapping[str, Any],
    settings: Settings,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """Persist the storage state so the next run can skip the browser.

    This file contains live session credentials. It is written with owner-only
    permissions and is listed in .gitignore; CI keeps it on the runner and never
    commits it.

    Typed as ``Mapping`` for the same reason as ``session_from_storage_state``:
    the caller passes Playwright's ``StorageState`` TypedDict directly.
    """
    path = settings.storage_state_path
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = dict(state)
    payload["_captured_at"] = datetime.now(UTC).isoformat()
    payload["_auth_headers"] = auth_headers or {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _capture_failure(page: Page, settings: Settings, label: str) -> None:
    """Save a screenshot and the DOM so a CI failure can be diagnosed later.

    A scheduled job fails while nobody is watching; without these, all that
    survives is a stack trace and a guess.
    """
    try:
        directory = settings.artifacts_dir
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

        screenshot = directory / f"{stamp}-{label}.png"
        page.screenshot(path=str(screenshot), full_page=True)

        html = directory / f"{stamp}-{label}.html"
        html.write_text(page.content(), encoding="utf-8")

        logger.error(
            "Captured failure artifacts",
            extra={"screenshot": str(screenshot), "html": str(html), "url": page.url},
        )
    except Exception as error:  # pragma: no cover - diagnostics must never mask the real error
        logger.warning("Could not capture failure artifacts", extra={"error": str(error)})


def clear_cached_session(path: Path) -> None:
    """Delete the cached session, forcing a fresh login on the next call."""
    path.unlink(missing_ok=True)
