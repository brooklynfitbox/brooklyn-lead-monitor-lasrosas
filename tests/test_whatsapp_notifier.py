"""Tests for the WhatsApp notifier, using a fake httpx.Client so no socket is
opened and no real Meta Graph API call is made."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from lead_monitor.config import Settings
from lead_monitor.models import Lead
from lead_monitor.whatsapp_notifier import WhatsAppError, WhatsAppNotifier


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://portal.example.com",
        "portal_username": "user",
        "portal_password": "portal-secret",
        "notify_channel": "whatsapp",
        "whatsapp_access_token": "EAAtoken",
        "whatsapp_phone_number_id": "1234567890",
        "whatsapp_to": "+34600000000",
        "whatsapp_template_name": "lead_alert",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


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


class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {"messages": [{"id": "wamid.abc"}]}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


class FakeClient:
    """Stands in for httpx.Client, recording every POST it was asked to make."""

    def __init__(self, response: FakeResponse | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self._response = response or FakeResponse()

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> FakeResponse:
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self._response


class TestSending(unittest.TestCase):
    def test_sends_one_request_per_recipient(self) -> None:
        client = FakeClient()
        notifier = WhatsAppNotifier(
            make_settings(whatsapp_to="+34600000000, +34600000001"),
            client_factory=lambda: client,
        )

        notifier.send([make_lead(i) for i in range(1, 4)])

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            [r["json"]["to"] for r in client.requests], ["+34600000000", "+34600000001"]
        )

    def test_one_batch_does_not_mean_one_message_per_lead(self) -> None:
        """Twelve leads still means one request per recipient, not twelve."""
        client = FakeClient()
        notifier = WhatsAppNotifier(make_settings(), client_factory=lambda: client)

        notifier.send([make_lead(i) for i in range(1, 13)])

        self.assertEqual(len(client.requests), 1)

    def test_uses_the_bearer_token_and_phone_number_id(self) -> None:
        client = FakeClient()
        notifier = WhatsAppNotifier(
            make_settings(
                whatsapp_access_token="secret-token", whatsapp_phone_number_id="999888777"
            ),
            client_factory=lambda: client,
        )

        notifier.send([make_lead()])

        request = client.requests[0]
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")
        self.assertIn("999888777", request["url"])

    def test_closes_the_client(self) -> None:
        client = FakeClient()
        WhatsAppNotifier(make_settings(), client_factory=lambda: client).send([make_lead()])
        self.assertTrue(client.closed)

    def test_empty_batch_opens_no_connection(self) -> None:
        opened = {"n": 0}

        def factory() -> FakeClient:
            opened["n"] += 1
            return FakeClient()

        WhatsAppNotifier(make_settings(), client_factory=factory).send([])  # type: ignore[arg-type]
        self.assertEqual(opened["n"], 0)

    def test_an_api_level_rejection_raises(self) -> None:
        """A 200 response whose body has no "messages" key means Meta rejected it."""
        client = FakeClient(response=FakeResponse(body={"error": {"message": "bad template"}}))
        notifier = WhatsAppNotifier(make_settings(), client_factory=lambda: client)

        with self.assertRaises(WhatsAppError):
            notifier.send([make_lead()])

    def test_an_http_level_failure_propagates(self) -> None:
        """The caller must not stamp leads as notified when delivery failed."""
        client = FakeClient(response=FakeResponse(status_code=500))
        notifier = WhatsAppNotifier(make_settings(), client_factory=lambda: client)

        with self.assertRaises(RuntimeError):
            notifier.send([make_lead()])


class TestPayload(unittest.TestCase):
    def _payload(self, leads: list[Lead], **settings_overrides: object) -> dict[str, Any]:
        client = FakeClient()
        WhatsAppNotifier(make_settings(**settings_overrides), client_factory=lambda: client).send(
            leads
        )
        result: dict[str, Any] = client.requests[0]["json"]
        return result

    def test_singular_heading(self) -> None:
        payload = self._payload([make_lead(1)])
        text = payload["template"]["components"][0]["parameters"][0]["text"]
        self.assertEqual(text, "1 lead nuevo en pre-order")

    def test_plural_heading_counts_them(self) -> None:
        payload = self._payload([make_lead(1), make_lead(2), make_lead(3)])
        text = payload["template"]["components"][0]["parameters"][0]["text"]
        self.assertEqual(text, "3 leads nuevos en pre-order")

    def test_no_personal_data_ever_appears(self) -> None:
        """Unlike email, this channel ignores NOTIFY_INCLUDE_PERSONAL_DATA —
        a template's parameters can't grow to fit an arbitrary contact list."""
        payload = self._payload(
            [make_lead(1, name="Ana Gómez", email="ana@example.com")],
            notify_include_personal_data=True,
        )
        rendered = str(payload)
        self.assertNotIn("Ana Gómez", rendered)
        self.assertNotIn("ana@example.com", rendered)

    def test_second_parameter_is_the_portal_link(self) -> None:
        payload = self._payload([make_lead(1)], portal_base_url="https://portal.example.com")
        text = payload["template"]["components"][0]["parameters"][1]["text"]
        self.assertEqual(text, "https://portal.example.com/#/leads")

    def test_template_name_and_language_come_from_settings(self) -> None:
        payload = self._payload(
            [make_lead(1)],
            whatsapp_template_name="brooklyn_leads",
            whatsapp_template_language="en_US",
        )
        self.assertEqual(payload["template"]["name"], "brooklyn_leads")
        self.assertEqual(payload["template"]["language"]["code"], "en_US")

    def test_messaging_product_is_whatsapp(self) -> None:
        payload = self._payload([make_lead(1)])
        self.assertEqual(payload["messaging_product"], "whatsapp")


class TestCenterName(unittest.TestCase):
    """CENTER_NAME set means the approved template has three placeholders,
    not two — see the module docstring."""

    def _payload(self, leads: list[Lead], **settings_overrides: object) -> dict[str, Any]:
        client = FakeClient()
        WhatsAppNotifier(make_settings(**settings_overrides), client_factory=lambda: client).send(
            leads
        )
        result: dict[str, Any] = client.requests[0]["json"]
        return result

    def test_unset_by_default_keeps_two_parameters(self) -> None:
        parameters = self._payload([make_lead(1)])["template"]["components"][0]["parameters"]
        self.assertEqual(len(parameters), 2)

    def test_set_prepends_a_third_parameter(self) -> None:
        parameters = self._payload([make_lead(1)], center_name="A Coruña — Pinar")["template"][
            "components"
        ][0]["parameters"]
        self.assertEqual(len(parameters), 3)
        self.assertEqual(parameters[0]["text"], "A Coruña — Pinar")

    def test_heading_and_link_shift_after_the_center_name(self) -> None:
        parameters = self._payload([make_lead(1), make_lead(2)], center_name="A Coruña — Pinar")[
            "template"
        ]["components"][0]["parameters"]
        self.assertEqual(parameters[1]["text"], "2 leads nuevos en pre-order")
        self.assertEqual(parameters[2]["text"], "https://portal.example.com/#/leads")


if __name__ == "__main__":
    unittest.main()
