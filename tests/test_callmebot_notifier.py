"""Tests for the CallMeBot notifier, using a fake httpx.Client so no socket is
opened and no real request reaches api.callmebot.com."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from lead_monitor.callmebot_notifier import CallMeBotNotifier
from lead_monitor.config import Settings
from lead_monitor.models import Lead


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "portal_base_url": "https://portal.example.com",
        "portal_username": "user",
        "portal_password": "portal-secret",
        "notify_channel": "callmebot",
        "callmebot_phone": "+34600000000",
        "callmebot_apikey": "123456",
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
    def __init__(self, status_code: int = 200, text: str = "Message queued. You will receive it in a few seconds.") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands in for httpx.Client, recording every GET it was asked to make."""

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

    def get(self, url: str, params: dict[str, Any]) -> FakeResponse:
        self.requests.append({"url": url, "params": params})
        return self._response


class TestSending(unittest.TestCase):
    def test_sends_one_request(self) -> None:
        client = FakeClient()
        notifier = CallMeBotNotifier(make_settings(), client_factory=lambda: client)

        notifier.send([make_lead(1)])

        self.assertEqual(len(client.requests), 1)

    def test_one_batch_does_not_mean_one_message_per_lead(self) -> None:
        """Twelve leads still means one request, not twelve."""
        client = FakeClient()
        notifier = CallMeBotNotifier(make_settings(), client_factory=lambda: client)

        notifier.send([make_lead(i) for i in range(1, 13)])

        self.assertEqual(len(client.requests), 1)

    def test_uses_the_configured_phone_and_apikey(self) -> None:
        client = FakeClient()
        notifier = CallMeBotNotifier(
            make_settings(callmebot_phone="+34611111111", callmebot_apikey="999888"),
            client_factory=lambda: client,
        )

        notifier.send([make_lead()])

        params = client.requests[0]["params"]
        self.assertEqual(params["phone"], "+34611111111")
        self.assertEqual(params["apikey"], "999888")

    def test_closes_the_client(self) -> None:
        client = FakeClient()
        CallMeBotNotifier(make_settings(), client_factory=lambda: client).send([make_lead()])
        self.assertTrue(client.closed)

    def test_empty_batch_opens_no_connection(self) -> None:
        opened = {"n": 0}

        def factory() -> FakeClient:
            opened["n"] += 1
            return FakeClient()

        CallMeBotNotifier(make_settings(), client_factory=factory).send([])  # type: ignore[arg-type]
        self.assertEqual(opened["n"], 0)

    def test_an_http_level_failure_propagates(self) -> None:
        """The caller must not stamp leads as notified when delivery failed."""
        client = FakeClient(response=FakeResponse(status_code=500))
        notifier = CallMeBotNotifier(make_settings(), client_factory=lambda: client)

        with self.assertRaises(RuntimeError):
            notifier.send([make_lead()])

    def test_an_unexpected_response_body_on_200_does_not_raise(self) -> None:
        """CallMeBot's response body on success/failure isn't documented (see
        the module docstring) — only the HTTP status is a checkable signal,
        so an odd-looking 200 body must not be treated as a delivery failure."""
        client = FakeClient(response=FakeResponse(text="unexpected text"))
        notifier = CallMeBotNotifier(make_settings(), client_factory=lambda: client)

        notifier.send([make_lead()])  # must not raise


class TestMessage(unittest.TestCase):
    def _params(self, leads: list[Lead], **settings_overrides: object) -> dict[str, Any]:
        client = FakeClient()
        CallMeBotNotifier(make_settings(**settings_overrides), client_factory=lambda: client).send(
            leads
        )
        result: dict[str, Any] = client.requests[0]["params"]
        return result

    def test_singular_heading(self) -> None:
        text = self._params([make_lead(1)])["text"]
        self.assertIn("1 lead nuevo en pre-order", text)

    def test_plural_heading_counts_them(self) -> None:
        text = self._params([make_lead(1), make_lead(2), make_lead(3)])["text"]
        self.assertIn("3 leads nuevos en pre-order", text)

    def test_no_personal_data_by_default(self) -> None:
        text = self._params([make_lead(1, name="Ana Gómez", phone="611111111")])["text"]
        self.assertNotIn("Ana Gómez", text)
        self.assertNotIn("611111111", text)

    def test_personal_data_included_when_opted_in(self) -> None:
        text = self._params(
            [make_lead(1, name="Ana Gómez", phone="611111111")],
            notify_include_personal_data=True,
        )["text"]
        self.assertIn("Ana Gómez", text)
        self.assertIn("611111111", text)

    def test_includes_the_portal_link(self) -> None:
        text = self._params([make_lead(1)], portal_base_url="https://portal.example.com")["text"]
        self.assertIn("https://portal.example.com/#/leads", text)

    def test_center_name_appears_when_set(self) -> None:
        text = self._params([make_lead(1)], center_name="A Coruña — Pinar")["text"]
        self.assertIn("A Coruña — Pinar", text)

    def test_center_name_omitted_when_unset(self) -> None:
        text = self._params([make_lead(1)])["text"]
        self.assertEqual(text.splitlines()[0], "🥊 Brooklyn Fitboxing")


if __name__ == "__main__":
    unittest.main()
