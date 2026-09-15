"""Tests for shipped configuration defaults."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from lead_monitor.config import LeadsClientKind, NotifyChannel, Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://myh4c.example.com/",
        "portal_username": "u",
        "portal_password": "p",
        "smtp_host": "smtp.example.com",
        "smtp_username": "u",
        "smtp_password": "p",
        "mail_from": "a@b.c",
        "mail_to": "d@e.f",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class TestDefaults(unittest.TestCase):
    def test_defaults_match_the_discovered_portal(self) -> None:
        s = _settings()
        self.assertEqual(s.leads_client, LeadsClientKind.GRAPHQL)
        self.assertEqual(s.leads_api_path, "/fs1")
        self.assertEqual(s.leads_status_filter, "pre-order")

    def test_recency_filter_is_on_by_default_for_madrid(self) -> None:
        """Shipped default: only recent pre-order leads notify, per Clarita's rule."""
        s = _settings()
        self.assertTrue(s.notify_recent_leads_only)
        self.assertEqual(s.notify_timezone, "Europe/Madrid")

    def test_login_timeout_is_generous_enough_for_a_cold_ci_runner(self) -> None:
        """15s (the original value) was measured too tight on GitHub Actions:
        a cold Playwright install with no warmed CDN cache took longer than
        that just to hydrate the login page's SPA bundle."""
        self.assertGreaterEqual(_settings().login_timeout_seconds, 30)

    def test_base_url_trailing_slash_is_stripped(self) -> None:
        self.assertEqual(_settings().leads_url(), "https://myh4c.example.com/fs1")

    def test_recipients_split_on_commas(self) -> None:
        self.assertEqual(_settings(mail_to="a@b.c, d@e.f").recipients, ["a@b.c", "d@e.f"])


class TestNotifyChannel(unittest.TestCase):
    """Each channel validates only the fields it actually needs, eagerly."""

    def test_email_is_the_default_channel(self) -> None:
        self.assertEqual(_settings().notify_channel, NotifyChannel.EMAIL)

    def test_whatsapp_does_not_require_smtp_fields(self) -> None:
        s = Settings(
            portal_base_url="https://p.example.com",
            portal_username="u",
            portal_password="p",
            notify_channel="whatsapp",
            whatsapp_access_token="EAAtoken",
            whatsapp_phone_number_id="1234567890",
            whatsapp_to="+34600000000",
            whatsapp_template_name="lead_alert",
        )  # type: ignore[call-arg]
        self.assertEqual(s.notify_channel, NotifyChannel.WHATSAPP)

    def test_whatsapp_without_a_token_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "WHATSAPP_ACCESS_TOKEN"):
            Settings(
                portal_base_url="https://p.example.com",
                portal_username="u",
                portal_password="p",
                notify_channel="whatsapp",
                whatsapp_phone_number_id="1234567890",
                whatsapp_to="+34600000000",
                whatsapp_template_name="lead_alert",
            )  # type: ignore[call-arg]

    def test_whatsapp_without_a_template_name_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "WHATSAPP_TEMPLATE_NAME"):
            Settings(
                portal_base_url="https://p.example.com",
                portal_username="u",
                portal_password="p",
                notify_channel="whatsapp",
                whatsapp_access_token="EAAtoken",
                whatsapp_phone_number_id="1234567890",
                whatsapp_to="+34600000000",
            )  # type: ignore[call-arg]

    def test_whatsapp_without_a_recipient_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "WHATSAPP_TO"):
            Settings(
                portal_base_url="https://p.example.com",
                portal_username="u",
                portal_password="p",
                notify_channel="whatsapp",
                whatsapp_access_token="EAAtoken",
                whatsapp_phone_number_id="1234567890",
                whatsapp_template_name="lead_alert",
            )  # type: ignore[call-arg]

    def test_email_without_smtp_settings_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "SMTP_HOST"):
            Settings(
                portal_base_url="https://p.example.com",
                portal_username="u",
                portal_password="p",
            )  # type: ignore[call-arg]

    def test_recipients_split_on_commas_for_whatsapp_too(self) -> None:
        s = Settings(
            portal_base_url="https://p.example.com",
            portal_username="u",
            portal_password="p",
            notify_channel="whatsapp",
            whatsapp_access_token="EAAtoken",
            whatsapp_phone_number_id="1234567890",
            whatsapp_to="+34600000000, +34600000001",
            whatsapp_template_name="lead_alert",
        )  # type: ignore[call-arg]
        self.assertEqual(s.whatsapp_recipients, ["+34600000000", "+34600000001"])


class TestMultipleChannels(unittest.TestCase):
    """NOTIFY_CHANNEL is comma-separated, so both email and WhatsApp can fire
    for the same batch — see notifier.CompositeNotifier."""

    def _both_channels(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "portal_base_url": "https://p.example.com",
            "portal_username": "u",
            "portal_password": "p",
            "notify_channel": "email,whatsapp",
            "smtp_host": "smtp.example.com",
            "smtp_username": "u",
            "smtp_password": "p",
            "mail_from": "a@b.c",
            "mail_to": "d@e.f",
            "whatsapp_access_token": "EAAtoken",
            "whatsapp_phone_number_id": "1234567890",
            "whatsapp_to": "+34600000000",
            "whatsapp_template_name": "lead_alert",
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    def test_single_value_parses_to_one_channel(self) -> None:
        self.assertEqual(_settings().notify_channels, [NotifyChannel.EMAIL])

    def test_comma_separated_parses_to_both_in_order(self) -> None:
        s = self._both_channels()
        self.assertEqual(s.notify_channels, [NotifyChannel.EMAIL, NotifyChannel.WHATSAPP])

    def test_whitespace_around_commas_is_tolerated(self) -> None:
        s = self._both_channels(notify_channel=" email , whatsapp ")
        self.assertEqual(s.notify_channels, [NotifyChannel.EMAIL, NotifyChannel.WHATSAPP])

    def test_both_channels_requires_both_sets_of_fields(self) -> None:
        with self.assertRaisesRegex(ValidationError, "WHATSAPP_ACCESS_TOKEN"):
            Settings(
                portal_base_url="https://p.example.com",
                portal_username="u",
                portal_password="p",
                notify_channel="email,whatsapp",
                smtp_host="smtp.example.com",
                smtp_username="u",
                smtp_password="p",
                mail_from="a@b.c",
                mail_to="d@e.f",
            )  # type: ignore[call-arg]

    def test_an_unknown_channel_name_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "NOTIFY_CHANNEL"):
            self._both_channels(notify_channel="email,carrier-pigeon")

    def test_blank_channel_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least one channel"):
            self._both_channels(notify_channel="")


if __name__ == "__main__":
    unittest.main()
