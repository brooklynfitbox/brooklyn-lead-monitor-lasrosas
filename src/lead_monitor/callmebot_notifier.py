"""WhatsApp notifications via CallMeBot — a free, unofficial self-notify
bridge, for whoever wants a WhatsApp alert without going through Meta's
official WhatsApp Business Cloud API first (see whatsapp_notifier.py).

CallMeBot (callmebot.com) is a small, free, community-run service. It is
not Meta's own infrastructure and comes with real trade-offs versus the
official API:

- **Self-notify only.** A CALLMEBOT_APIKEY is issued to one specific phone
  number, and CallMeBot only ever delivers to that same number — there is
  no way to fan a message out to someone else's phone. That fits this
  project's actual ask (an internal team alert landing back on the phone
  that opted in), not a customer-facing broadcast.
- **No Meta account, app, System User or approved template needed.** The
  whole setup is: add the CallMeBot contact on the receiving phone, send it
  one fixed opt-in message, and it replies with the API key. Minutes, not
  the days a template can take to get approved.
- **Best-effort, not SLA-backed.** It is a free hobby-scale service with no
  guarantee of uptime or delivery — a reasonable stopgap while the official
  route is blocked (Meta Business Manager access, a number already in
  production use elsewhere, template approval pending), not a permanent
  replacement for it. Email (notifier.py) remains the channel this project
  treats as authoritative; CallMeBot is a bonus, not the safety net.
- **Text only.** No templates, no structured components — just a plain
  string, so (unlike whatsapp_notifier.py) there is no approved-shape
  constraint to respect. NOTIFY_INCLUDE_PERSONAL_DATA is still honoured,
  the same as email, since nothing here restricts the message's shape.

One request per run, not one per recipient — CALLMEBOT_PHONE names exactly
one phone (the one behind CALLMEBOT_APIKEY), unlike WHATSAPP_TO / MAIL_TO
which can be a list. Running the same center against more than one
recipient would need more than one API key, i.e. more than one deployment
of this channel — not supported here because nothing in this project's
brief currently needs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import httpx

from .config import Settings
from .models import Lead

logger = logging.getLogger(__name__)

_API_URL = "https://api.callmebot.com/whatsapp.php"


class CallMeBotNotifier:
    """Sends one plain-text WhatsApp message per run via CallMeBot.

    ``client_factory`` exists so tests can substitute a fake without opening
    a socket, the same shape as ``SmtpNotifier``'s ``transport_factory`` and
    ``WhatsAppNotifier``'s ``client_factory``.
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
        params = {
            "phone": settings.callmebot_phone,
            "text": self._message(leads),
            "apikey": settings.callmebot_apikey.get_secret_value(),
        }

        with self._client_factory() as client:
            response = client.get(_API_URL, params=params)
            response.raise_for_status()
            # CallMeBot has no published API reference documenting the exact
            # response body on success or failure (checked callmebot.com's
            # blog and FAQ — neither specifies one), so unlike whatsapp_notifier.py
            # this does not try to parse the body to decide success/failure.
            # HTTP 200 is the one behaviour CallMeBot documents as "request
            # accepted"; the body is logged at DEBUG so a human can eyeball it
            # if a message doesn't arrive, but it is not treated as a machine-
            # checkable success/failure signal — guessing at undocumented text
            # risks either false failures (leads never marked notified, so
            # every run re-sends) or false successes (a real failure looks
            # like it worked and nobody hears about it), and there is no way
            # to know which without a documented contract.
            logger.debug("CallMeBot response", extra={"body": response.text.strip()[:200]})

        logger.info("Notification sent", extra={"leads": len(leads)})

    def _message(self, leads: Sequence[Lead]) -> str:
        count = len(leads)
        heading = "1 lead nuevo en pre-order" if count == 1 else f"{count} leads nuevos en pre-order"
        center = self._settings.center_name

        lines = [f"🥊 Brooklyn Fitboxing — {center}" if center else "🥊 Brooklyn Fitboxing"]
        lines.append(heading)
        if self._settings.notify_include_personal_data:
            for lead in leads:
                lines.append(f"• {lead.name or '(sin nombre)'} — {lead.phone or 'sin teléfono'}")
        lines.append(f"Portal: {self._settings.leads_page_url()}")
        return "\n".join(lines)
