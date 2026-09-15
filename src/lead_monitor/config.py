"""Typed configuration loaded from the environment.

Every credential the monitor needs arrives as an environment variable: from a
local ``.env`` file during development, from GitHub Secrets in CI. Nothing is
read from a config file that could be committed by accident.

Validation is deliberately eager. A run that is missing its SMTP password
should fail immediately with a clear message rather than fetch leads, discover
new ones, and only then blow up at the point of sending — which would leave the
database claiming leads were seen while no email ever arrived.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LeadsClientKind(StrEnum):
    """Which strategy fetches the leads list."""

    GRAPHQL = "graphql"
    API = "api"
    DOM = "dom"


class NotifyChannel(StrEnum):
    """Where a new-lead alert goes. See notifier.py / whatsapp_notifier.py /
    callmebot_notifier.py."""

    EMAIL = "email"
    WHATSAPP = "whatsapp"
    CALLMEBOT = "callmebot"


class Settings(BaseSettings):
    """All runtime configuration, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Identity -----------------------------------------------------------
    # A short label for which club this deployment watches, e.g. "A Coruña".
    # Meaningless when there is exactly one deployment and one recipient —
    # one inbox, or one WhatsApp destination — which is why it is optional
    # and blank by default. It stops being optional in practice the moment
    # more than one center's monitor sends to the same destination: without
    # it every alert looks identical (same email subject/sender, or the same
    # WhatsApp template text) and the only way to tell centers apart is
    # opening the message and reading the portal link's domain. Set, it
    # prefixes the email subject and sender name (notifier.py), and becomes
    # the WhatsApp template's leading parameter (whatsapp_notifier.py) — note
    # that one changes the template's required shape, see README.
    center_name: str = ""

    # --- Portal -----------------------------------------------------------
    portal_base_url: Annotated[str, Field(min_length=1)]
    portal_username: Annotated[str, Field(min_length=1)]
    portal_password: SecretStr

    leads_client: LeadsClientKind = LeadsClientKind.GRAPHQL
    leads_api_path: str = "/fs1"

    # The GraphQL document that fetches the leads. Left blank, the client falls
    # back to a built-in guess and says so in the log. Run
    # `lead-monitor introspect` against the portal to get the exact query — it
    # reads only the schema, never a single customer record.
    leads_graphql_query: str = ""
    leads_graphql_operation: str = ""
    leads_graphql_variables: str = "{}"

    # Only notify about leads in this status. 'pre-order' is what an unhandled
    # lead shows before a member of staff opens it — the moment worth alerting
    # on. Matching ignores case and punctuation, so 'pre-order', 'Pre Order'
    # and 'PREORDER' are equivalent.
    #
    # Trade-off, chosen deliberately: a lead created and then handled inside a
    # single ten-minute polling gap will have left 'pre-order' by the time the
    # monitor looks, so it is never notified. Set this blank to notify about
    # every new lead regardless of status and remove that gap. See
    # clients.base.matches_status.
    leads_status_filter: str = "pre-order"

    # A pre-order lead that never converted can sit on the portal for weeks.
    # Without this, the first time the database meets one it reads as "new"
    # and gets mailed — exactly the backlog noise this exists to prevent. On,
    # notification is limited to leads created since NOTIFY_TIMEZONE's start
    # of yesterday, or since Saturday if today is Monday (nobody is watching
    # the portal over the weekend, so Monday also covers it). Leads outside
    # the window are still recorded, so they are never flagged as new again
    # later — they are just never emailed. See recency.py.
    notify_recent_leads_only: bool = True

    # Timezone the "yesterday" / "Monday means also the weekend" rule above is
    # evaluated in. Everything else in this codebase runs on UTC timestamps;
    # this is the one place a calendar day matters, and Brooklyn Fitboxing's
    # leads are worked in Spain.
    notify_timezone: str = "Europe/Madrid"

    # Pages Playwright needs to reach. Paths, relative to portal_base_url.
    # The portal is a hash-routed single-page app, so these carry the '#'.
    # The leads path is confirmed; the login path is a sensible guess — if the
    # login form is elsewhere, set PORTAL_LOGIN_PATH and the browser step finds
    # it there instead.
    portal_login_path: str = "/#/login"
    portal_leads_path: str = "/#/leads"

    # Login form selectors. Left blank, auth.py locates the fields by looking
    # for the password input and the text input preceding it, which works on
    # most portals. Set them explicitly when the heuristic picks wrong — that
    # is a configuration change rather than a code change.
    login_username_selector: str = ""
    login_password_selector: str = ""
    login_submit_selector: str = ""
    # A selector that only exists once login succeeded. Without it, success is
    # inferred from having navigated away from the login page, which is weaker.
    login_success_selector: str = ""
    # How long to wait for the password field to appear before giving up.
    # 15s was too tight: a GitHub Actions runner with a cold Playwright
    # install and no warmed CDN cache took longer than that just to hydrate
    # the login page's SPA bundle, and the run failed on that alone before
    # ever reaching a wrong-credentials check.
    login_timeout_seconds: Annotated[int, Field(ge=1)] = 45

    # Where the cached browser session is kept between runs.
    storage_state_path: Path = Path("storage_state.json")
    storage_state_max_age_minutes: Annotated[int, Field(ge=1)] = 120

    headless: bool = True
    artifacts_dir: Path = Path("artifacts")

    # Which installed browser Playwright drives. Blank (the default) uses
    # Playwright's own bundled Chromium — the normal case on Ubuntu/Debian,
    # where `playwright install chromium` is officially supported. Set to
    # "chrome" on a host where that install path isn't supported (RHEL-family
    # distros — AlmaLinux, Rocky — lack an official Playwright dependency
    # installer) and a system-wide Google Chrome is installed instead
    # (`dnf install google-chrome-stable`); Playwright then drives that
    # binary via its documented `channel` option instead of its own download.
    browser_channel: str = ""

    # --- Notification -------------------------------------------------------
    # Which channel(s) carry the alert. A single value ("email") or a
    # comma-separated list ("email,whatsapp") to fire more than one channel
    # per batch — see notify_channels below. Only the fields the selected
    # channel(s) need are required — see
    # _require_fields_for_the_chosen_channels below.
    notify_channel: str = "email"

    # --- Email (NOTIFY_CHANNEL=email) --------------------------------------
    smtp_host: str = ""
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    mail_from: str = ""
    mail_to: str = ""

    # --- WhatsApp (NOTIFY_CHANNEL=whatsapp) ---------------------------------
    # WhatsApp Business Cloud API (Meta's official API — this is not the
    # personal-account automation that risks a ban; it needs a WhatsApp
    # Business Platform app in Meta Business Manager). A permanent access
    # token from a System User, and the Phone Number ID of the sending
    # number, both from developers.facebook.com.
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    # Recipient phone number(s) in E.164 (e.g. +34600123456), comma-separated
    # for more than one — same shape as MAIL_TO.
    whatsapp_to: str = ""
    # Name of a message template pre-approved in Meta Business Manager. A
    # business-initiated WhatsApp message outside a 24-hour reply window must
    # use an approved template — free-form text is rejected — so this can't
    # default to anything; see README for the exact template text to submit.
    whatsapp_template_name: str = ""
    whatsapp_template_language: str = "es"
    # Graph API version. Meta retires old versions on its own schedule
    # (typically ~2 years), so this is a variable rather than a constant —
    # bumping it is a config change, not a code change.
    whatsapp_api_version: str = "v21.0"

    # --- CallMeBot (NOTIFY_CHANNEL=callmebot) -------------------------------
    # A free, unofficial, self-notify-only WhatsApp bridge (callmebot.com) —
    # a stopgap for a real WhatsApp Business Cloud API connection (see the
    # WHATSAPP_* settings above), useful whenever that official route is
    # blocked or delayed (Meta Business Manager access, template approval,
    # a number already in production use elsewhere). No Meta account, app or
    # approved template needed: whoever should receive the alert adds the
    # CallMeBot contact on their own phone and opts in once; that hands back
    # an API key tied to that phone number, which is CALLMEBOT_APIKEY below.
    # See callmebot_notifier.py for the mechanics and its real limits (one
    # phone per key, text-only, best-effort — not Meta's own infrastructure).
    callmebot_phone: str = ""
    callmebot_apikey: SecretStr = SecretStr("")

    # When False (the default), a notification carries no personal data —
    # only how many leads arrived, the exact time each came in, a reference
    # id and a portal link. This applies to whichever channel is active:
    # customer names, emails and phones are kept out of the email, and read
    # from the portal instead. It also controls what the SQLite database
    # stores, since that file is committed to git — see store.LeadStore. Set
    # True to include full contact details everywhere this setting reaches.
    #
    # WhatsApp is the one exception: a template message's parameters are
    # fixed in number and shape by whatever was approved in Meta Business
    # Manager, so it cannot grow to fit an arbitrary per-lead contact list
    # the way the email body can. The WhatsApp message always carries only a
    # count and a portal link, regardless of this setting — see
    # whatsapp_notifier.py.
    notify_include_personal_data: bool = False

    # --- Behaviour --------------------------------------------------------
    database_path: Path = Path("state/leads.db")
    log_level: str = "INFO"
    log_plain: bool = False
    seed_without_notifying: bool = True
    request_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 4

    @field_validator("portal_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("leads_api_path", "portal_login_path", "portal_leads_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @model_validator(mode="after")
    def _require_fields_for_the_chosen_channels(self) -> Settings:
        """Fail at startup, not at send time, on a half-configured channel.

        Mirrors the module docstring's philosophy: a run that is missing what
        it needs should say so immediately, before it has fetched leads and
        recorded them as seen with no way left to announce them. Runs for
        every selected channel independently — enabling a second channel
        never relaxes what the first one needs.
        """
        try:
            channels = self.notify_channels
        except ValueError as error:
            raise ValueError(
                f"NOTIFY_CHANNEL: {error} (valid values: "
                f"{', '.join(c.value for c in NotifyChannel)})"
            ) from error
        if not channels:
            raise ValueError("NOTIFY_CHANNEL must name at least one channel")

        if NotifyChannel.EMAIL in channels:
            missing = [
                name
                for name, value in (
                    ("SMTP_HOST", self.smtp_host),
                    ("SMTP_USERNAME", self.smtp_username),
                    ("SMTP_PASSWORD", self.smtp_password.get_secret_value()),
                    ("MAIL_FROM", self.mail_from),
                    ("MAIL_TO", self.mail_to),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"NOTIFY_CHANNEL=email requires: {', '.join(missing)}")
        if NotifyChannel.WHATSAPP in channels:
            missing = [
                name
                for name, value in (
                    ("WHATSAPP_ACCESS_TOKEN", self.whatsapp_access_token.get_secret_value()),
                    ("WHATSAPP_PHONE_NUMBER_ID", self.whatsapp_phone_number_id),
                    ("WHATSAPP_TO", self.whatsapp_to),
                    ("WHATSAPP_TEMPLATE_NAME", self.whatsapp_template_name),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"NOTIFY_CHANNEL=whatsapp requires: {', '.join(missing)}")
        if NotifyChannel.CALLMEBOT in channels:
            missing = [
                name
                for name, value in (
                    ("CALLMEBOT_PHONE", self.callmebot_phone),
                    ("CALLMEBOT_APIKEY", self.callmebot_apikey.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"NOTIFY_CHANNEL=callmebot requires: {', '.join(missing)}")
        return self

    @property
    def notify_channels(self) -> list[NotifyChannel]:
        """NOTIFY_CHANNEL parsed as a comma-separated list.

        "email" fires just the one channel, unchanged from before this
        existed. "email,whatsapp" fires both for every batch — see
        notifier.build_notifier and notifier.CompositeNotifier.
        """
        return [
            NotifyChannel(token.strip())
            for token in self.notify_channel.split(",")
            if token.strip()
        ]

    @property
    def recipients(self) -> list[str]:
        """MAIL_TO parsed as a comma-separated list."""
        return [address.strip() for address in self.mail_to.split(",") if address.strip()]

    @property
    def whatsapp_recipients(self) -> list[str]:
        """WHATSAPP_TO parsed as a comma-separated list."""
        return [number.strip() for number in self.whatsapp_to.split(",") if number.strip()]

    def leads_url(self) -> str:
        """Absolute URL of the leads JSON endpoint."""
        return f"{self.portal_base_url}{self.leads_api_path}"

    def login_url(self) -> str:
        """Absolute URL of the login page."""
        return f"{self.portal_base_url}{self.portal_login_path}"

    def leads_page_url(self) -> str:
        """Absolute URL of the human-facing Leads page."""
        return f"{self.portal_base_url}{self.portal_leads_path}"

    def secret_values(self) -> list[str]:
        """Every secret string, for the logging redaction filter.

        Includes the WhatsApp access token even when the email channel is
        active: it can end up in an exception message or an httpx log line
        regardless of which channel actually sent anything.
        """
        return [
            value
            for value in (
                self.portal_password.get_secret_value(),
                self.smtp_password.get_secret_value(),
                self.whatsapp_access_token.get_secret_value(),
                self.callmebot_apikey.get_secret_value(),
            )
            if value
        ]


def load_settings() -> Settings:
    """Build :class:`Settings`, raising a readable error when something is missing."""
    return Settings()  # type: ignore[call-arg]
