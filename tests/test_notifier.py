"""Tests for the SMTP notifier, using a fake transport so no socket is opened."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from email.message import EmailMessage
from types import TracebackType

from lead_monitor.config import Settings
from lead_monitor.models import Lead
from lead_monitor.notifier import CompositeNotifier, SmtpNotifier, build_notifier
from lead_monitor.whatsapp_notifier import WhatsAppNotifier


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://portal.example.com/",
        "portal_username": "user",
        "portal_password": "portal-secret",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "mailer@example.com",
        "smtp_password": "smtp-secret",
        "mail_from": "mailer@example.com",
        "mail_to": "ops@example.com",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class FakeTransport:
    """Stands in for smtplib.SMTP, recording what it was asked to do."""

    def __init__(self, fail_on_send: Exception | None = None) -> None:
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []
        self.closed = False
        self._fail_on_send = fail_on_send

    def __enter__(self) -> FakeTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))

    def send_message(self, message: EmailMessage) -> None:
        if self._fail_on_send is not None:
            raise self._fail_on_send
        self.sent.append(message)


def make_lead(n: int = 1, **overrides: object) -> Lead:
    values: dict[str, object] = {
        "external_id": str(n),
        "name": f"Lead {n}",
        "email": f"lead{n}@example.com",
        "phone": "600123456",
        "status": "Pre Order",
        "club": "Brooklyn Madrid",
        "created_at": datetime(2026, 7, 29, 9, 30, tzinfo=UTC),
    }
    values.update(overrides)
    return Lead(**values)  # type: ignore[arg-type]


class TestSending(unittest.TestCase):
    def test_sends_one_message_for_the_whole_batch(self) -> None:
        """Twelve leads must not mean twelve emails."""
        transport = FakeTransport()
        notifier = SmtpNotifier(make_settings(), transport_factory=lambda: transport)

        notifier.send([make_lead(i) for i in range(1, 13)])

        self.assertEqual(len(transport.sent), 1)

    def test_authenticates_before_sending(self) -> None:
        transport = FakeTransport()
        notifier = SmtpNotifier(make_settings(), transport_factory=lambda: transport)

        notifier.send([make_lead()])

        self.assertEqual(transport.logins, [("mailer@example.com", "smtp-secret")])

    def test_closes_the_connection(self) -> None:
        transport = FakeTransport()
        SmtpNotifier(make_settings(), transport_factory=lambda: transport).send([make_lead()])
        self.assertTrue(transport.closed)

    def test_empty_batch_opens_no_connection(self) -> None:
        opened = {"n": 0}

        def factory() -> FakeTransport:
            opened["n"] += 1
            return FakeTransport()

        SmtpNotifier(make_settings(), transport_factory=factory).send([])  # type: ignore[arg-type]
        self.assertEqual(opened["n"], 0)

    def test_a_failed_send_propagates(self) -> None:
        """The caller must not stamp leads as notified when delivery failed."""
        transport = FakeTransport(fail_on_send=OSError("connection reset"))
        notifier = SmtpNotifier(make_settings(), transport_factory=lambda: transport)

        with self.assertRaises(OSError):
            notifier.send([make_lead()])

        self.assertEqual(transport.sent, [])


class TestMessage(unittest.TestCase):
    def setUp(self) -> None:
        self.notifier = SmtpNotifier(make_settings(), transport_factory=FakeTransport)

    def test_singular_subject_shows_the_time_not_the_name(self) -> None:
        """Default is redacted: the subject must not carry the person's name."""
        message = self.notifier.build_message([make_lead(1, name="Ana Gómez")])
        self.assertNotIn("Ana", message["Subject"])
        self.assertIn("2026-07-29 09:30:00", message["Subject"])

    def test_plural_subject_counts_them(self) -> None:
        message = self.notifier.build_message([make_lead(1), make_lead(2), make_lead(3)])
        self.assertEqual(message["Subject"], "3 leads nuevos en pre-order")

    def test_is_multipart_with_text_and_html(self) -> None:
        message = self.notifier.build_message([make_lead()])
        types = {part.get_content_type() for part in message.walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)

    def test_marks_itself_as_automated(self) -> None:
        """Stops vacation responders bouncing back at the monitor."""
        message = self.notifier.build_message([make_lead()])
        self.assertEqual(message["Auto-Submitted"], "auto-generated")

    def test_addresses_every_recipient(self) -> None:
        notifier = SmtpNotifier(
            make_settings(mail_to="a@example.com, b@example.com"),
            transport_factory=FakeTransport,
        )
        message = notifier.build_message([make_lead()])
        self.assertEqual(message["To"], "a@example.com, b@example.com")

    def test_no_personal_data_in_the_default_email(self) -> None:
        """The whole point of the redacted default: PII stays in the portal."""
        message = self.notifier.build_message([make_lead(1, name="Ana Gómez")])
        for part in message.walk():
            if part.get_content_type().startswith("text/"):
                content = part.get_content()
                self.assertNotIn("Ana Gómez", content)
                self.assertNotIn("lead1@example.com", content)
                self.assertNotIn("600123456", content)

    def test_the_exact_arrival_time_appears(self) -> None:
        """With seconds — the request was the exact time it came in."""
        message = self.notifier.build_message([make_lead(1)])
        text = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
        self.assertIn("2026-07-29 09:30:00", text)

    def test_a_reference_lets_staff_find_the_lead(self) -> None:
        message = self.notifier.build_message([make_lead(1)])
        text = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
        self.assertIn("1", text)  # external_id

    def test_the_full_mode_includes_contact_details(self) -> None:
        """Opt-in: NOTIFY_INCLUDE_PERSONAL_DATA=true restores the old email."""
        notifier = SmtpNotifier(
            make_settings(notify_include_personal_data=True), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1, name="Ana Gómez")])
        text = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
        self.assertIn("Ana Gómez", text)
        self.assertIn("lead1@example.com", text)

    def test_html_escapes_lead_supplied_values_in_full_mode(self) -> None:
        """Lead data is attacker-influenced free text; it must not become markup."""
        notifier = SmtpNotifier(
            make_settings(notify_include_personal_data=True), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1, name="<script>alert('x')</script>")])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_missing_fields_render_as_a_dash_in_full_mode(self) -> None:
        notifier = SmtpNotifier(
            make_settings(notify_include_personal_data=True), transport_factory=FakeTransport
        )
        message = notifier.build_message([Lead(external_id="1", name="Ana", status="pre-order")])
        body = message.get_body(preferencelist=("plain",))
        assert body is not None
        self.assertIn("—", body.get_content())


class TestCenterName(unittest.TestCase):
    """CENTER_NAME disambiguates alerts once more than one deployment shares
    an inbox — see Settings.center_name."""

    def test_unset_by_default_looks_exactly_as_before(self) -> None:
        notifier = SmtpNotifier(make_settings(), transport_factory=FakeTransport)
        message = notifier.build_message([make_lead(1)])
        self.assertEqual(message["Subject"], "Lead nuevo en pre-order — 2026-07-29 09:30:00")
        self.assertIn("Brooklyn Lead Monitor <mailer@example.com>", message["From"])

    def test_prefixes_the_subject(self) -> None:
        notifier = SmtpNotifier(
            make_settings(center_name="A Coruña"), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1), make_lead(2)])
        self.assertEqual(message["Subject"], "[A Coruña] 2 leads nuevos en pre-order")

    def test_appears_in_the_sender_name(self) -> None:
        notifier = SmtpNotifier(
            make_settings(center_name="A Coruña"), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1)])
        self.assertIn("Brooklyn Lead Monitor — A Coruña", message["From"])

    def test_appears_in_the_plain_body(self) -> None:
        notifier = SmtpNotifier(
            make_settings(center_name="A Coruña"), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1)])
        text = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
        self.assertIn("Centro: A Coruña", text)

    def test_appears_in_the_html_header(self) -> None:
        notifier = SmtpNotifier(
            make_settings(center_name="A Coruña"), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1)])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]
        self.assertIn("Lead Monitor · A Coruña", html)

    def test_appears_as_a_highlighted_badge_in_the_html_body(self) -> None:
        """Not just the small header subtitle — a badge next to "Pre Order",
        in the body itself, so it's visible without having to notice
        secondary text."""
        notifier = SmtpNotifier(
            make_settings(center_name="A Coruña"), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1)])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]
        # Appears twice: once in the header subtitle, once as the body badge.
        self.assertEqual(html.count("A Coruña"), 2)
        self.assertIn("Pre Order", html)

    def test_unset_shows_no_center_badge(self) -> None:
        notifier = SmtpNotifier(make_settings(), transport_factory=FakeTransport)
        message = notifier.build_message([make_lead(1)])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]
        # Only the Pre Order badge, unchanged from before CENTER_NAME existed.
        self.assertIn("Pre Order", html)


class TestBranding(unittest.TestCase):
    """The HTML mail is Brooklyn-branded; the plain-text fallback carries no styling."""

    def setUp(self) -> None:
        self.notifier = SmtpNotifier(make_settings(), transport_factory=FakeTransport)

    def _html(self, leads: list[Lead]) -> str:
        message = self.notifier.build_message(leads)
        body = message.get_body(preferencelist=("html",))
        assert body is not None
        return body.get_content()  # type: ignore[return-value]

    def test_the_brand_name_and_colour_are_present(self) -> None:
        html = self._html([make_lead(1)])
        self.assertIn("BROOKLYN", html)
        self.assertIn("FITBOXING", html)
        self.assertIn("#FFD400", html)

    def test_links_to_the_real_portal_leads_page(self) -> None:
        notifier = SmtpNotifier(
            make_settings(portal_base_url="https://portal.example.com"),
            transport_factory=FakeTransport,
        )
        message = notifier.build_message([make_lead(1)])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]
        self.assertIn("https://portal.example.com/#/leads", html)

    def test_the_redacted_html_carries_no_personal_data_either(self) -> None:
        html = self._html([make_lead(1, name="Ana Gómez", email="ana@example.com")])
        self.assertNotIn("Ana Gómez", html)
        self.assertNotIn("ana@example.com", html)

    def test_branding_also_appears_in_full_mode(self) -> None:
        notifier = SmtpNotifier(
            make_settings(notify_include_personal_data=True), transport_factory=FakeTransport
        )
        message = notifier.build_message([make_lead(1)])
        html = message.get_body(preferencelist=("html",)).get_content()  # type: ignore[union-attr]
        self.assertIn("BROOKLYN", html)


class TestBuildNotifier(unittest.TestCase):
    """NOTIFY_CHANNEL picks the implementation; nothing else should have to."""

    def test_email_is_the_default(self) -> None:
        self.assertIsInstance(build_notifier(make_settings()), SmtpNotifier)

    def test_whatsapp_is_selected_explicitly(self) -> None:
        settings = make_settings(
            notify_channel="whatsapp",
            whatsapp_access_token="EAAtoken",
            whatsapp_phone_number_id="1234567890",
            whatsapp_to="+34600000000",
            whatsapp_template_name="lead_alert",
            # These SMTP fields aren't required for this channel; the base
            # make_settings() still supplies them, which is fine — extra
            # unused settings are harmless.
        )
        self.assertIsInstance(build_notifier(settings), WhatsAppNotifier)

    def test_both_channels_returns_a_composite(self) -> None:
        settings = make_settings(
            notify_channel="email,whatsapp",
            whatsapp_access_token="EAAtoken",
            whatsapp_phone_number_id="1234567890",
            whatsapp_to="+34600000000",
            whatsapp_template_name="lead_alert",
        )
        self.assertIsInstance(build_notifier(settings), CompositeNotifier)


class TestCompositeNotifier(unittest.TestCase):
    """Fires every configured channel per batch — see notify_channel=email,whatsapp."""

    def test_sends_to_every_notifier_in_order(self) -> None:
        calls: list[str] = []

        class Recorder:
            def __init__(self, label: str) -> None:
                self.label = label

            def send(self, leads: list[Lead]) -> None:
                calls.append(self.label)

        CompositeNotifier([Recorder("email"), Recorder("whatsapp")]).send([make_lead()])  # type: ignore[list-item]

        self.assertEqual(calls, ["email", "whatsapp"])

    def test_a_failure_on_one_channel_still_attempts_the_rest(self) -> None:
        calls: list[str] = []

        class Failing:
            def send(self, leads: list[Lead]) -> None:
                calls.append("failing")
                raise RuntimeError("email server down")

        class Recorder:
            def send(self, leads: list[Lead]) -> None:
                calls.append("whatsapp")

        with self.assertRaises(RuntimeError):
            CompositeNotifier([Failing(), Recorder()]).send([make_lead()])  # type: ignore[list-item]

        # Both were attempted — a failure on the first doesn't skip the rest —
        # but the call still raises, so the caller never marks the batch
        # notified and both channels are retried next run.
        self.assertEqual(calls, ["failing", "whatsapp"])


if __name__ == "__main__":
    unittest.main()
