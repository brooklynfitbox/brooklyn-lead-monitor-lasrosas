"""WhatsApp notifications for newly discovered leads, via WhatsApp Business.

An alternative to email (see notifier.py) for a channel that lands as an
ordinary phone push notification. This uses the official WhatsApp Business
Cloud API (Meta's own platform, reached through a WhatsApp Business account
in Meta Business Manager) — not personal-account browser automation, which
violates WhatsApp's terms and risks the number being banned, and which is a
poor fit besides: it wants a live, logged-in session between runs, which a
stateless CI job restarting every ten minutes does not have. Selected with
``NOTIFY_CHANNEL=whatsapp``.

One structural difference from both email and a plain bot API (Telegram's,
for instance): the Cloud API will not deliver free-form text unless the
recipient messaged the business within the last 24 hours — reasonable
per-message anti-spam policy, but nothing this monitor can promise between
unattended ten-minute runs. A *business-initiated* message like this one
must instead use a template pre-approved in Meta Business Manager, with a
fixed number of ``{{n}}`` text placeholders. See README for the exact
template text to submit for approval.

That approval mechanic is also why this channel does not honour
``NOTIFY_INCLUDE_PERSONAL_DATA`` the way email does. A template's parameter
count and shape are fixed at approval time; there is no way to grow it to
fit however many leads a given run found, each with its own name, email and
phone. So the WhatsApp message always carries a count and a portal link,
nothing more — the safer of the two email modes, always. See
``Settings.notify_include_personal_data`` for the full policy across
channels and the database.

``CENTER_NAME`` (see ``notifier.py``'s email use of it) is honoured here too,
but it changes the *shape* of the template rather than just its content: set,
it becomes the template's first parameter, so the approved template needs
three ``{{n}}`` placeholders instead of two — see README. Left unset (the
right choice whenever this deployment is the only one sending to its
``WHATSAPP_TO`` number), the template stays two placeholders, unchanged from
before this existed.

Same non-atomicity trade-off as documented on the Telegram-shaped channel
this replaced: SMTP's ``send_message`` delivers to every recipient in one
atomic call; the Cloud API has no equivalent, so each recipient is a
separate HTTP request, and a partial failure mid-loop means the next run's
retry can double-deliver to whichever recipient already succeeded. Accepted
for the same reason as everywhere else in this project: a duplicate is an
annoyance, a silently dropped lead is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import httpx

from .config import Settings
from .models import Lead

logger = logging.getLogger(__name__)

_API_BASE = "https://graph.facebook.com"


class WhatsAppError(RuntimeError):
    """The Cloud API accepted the HTTP request but rejected the message."""


class WhatsAppNotifier:
    """Posts one template message per run to every configured recipient.

    ``client_factory`` exists so tests can substitute a fake without opening
    a socket, the same shape as ``SmtpNotifier``'s ``transport_factory``.
    """

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.Client:
        return httpx.Client(timeout=self._settings.request_timeout_seconds)

    def send(self, leads: Sequence[Lead]) -> None:
        if not leads:
            logger.debug("Nothing to notify")
            return

        settings = self._settings
        token = settings.whatsapp_access_token.get_secret_value()
        url = (
            f"{_API_BASE}/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        headers = {"Authorization": f"Bearer {token}"}
        payload_template = {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": settings.whatsapp_template_name,
                "language": {"code": settings.whatsapp_template_language},
                "components": [{"type": "body", "parameters": self._parameters(leads)}],
            },
        }
        recipients = settings.whatsapp_recipients

        with self._client_factory() as client:
            for to in recipients:
                response = client.post(url, headers=headers, json={**payload_template, "to": to})
                response.raise_for_status()
                body = response.json()
                if "messages" not in body:
                    raise WhatsAppError(f"WhatsApp API rejected the message: {body}")

        logger.info("Notification sent", extra={"leads": len(leads), "recipients": len(recipients)})

    def _parameters(self, leads: Sequence[Lead]) -> list[dict[str, str]]:
        """The template's body parameters, in {{n}} order.

        Two by default: heading, portal link. CENTER_NAME set prepends a
        third — the approved template must match whichever shape this
        deployment uses; see the module docstring.
        """
        parameters = [
            {"type": "text", "text": self._heading(len(leads))},
            {"type": "text", "text": self._settings.leads_page_url()},
        ]
        center = self._settings.center_name
        if center:
            parameters.insert(0, {"type": "text", "text": center})
        return parameters

    @staticmethod
    def _heading(count: int) -> str:
        return "1 lead nuevo en pre-order" if count == 1 else f"{count} leads nuevos en pre-order"
