"""Email notification for newly discovered leads.

One email per run covering every new lead, not one per lead. A club that runs a
campaign can pick up a dozen leads in a ten-minute window, and twelve separate
messages is how a useful alert becomes something people filter into a folder and
stop reading.

The send is deliberately all-or-nothing. ``send`` either returns normally, in
which case the mail server accepted the whole batch and every lead in it may be
stamped as notified, or it raises and none of them are. Partial success would
mean guessing which leads made it, and a wrong guess loses one silently.

The HTML body is branded (black/yellow, Brooklyn Fitboxing's colours) so the
person reading it recognises it as their own tool at a glance rather than a
generic system alert. The palette is a best-effort match — brooklynfitboxing.com
blocked automated fetching of its stylesheet, so the exact brand hex codes
were not available; swap ``_BRAND_YELLOW`` below if the real one differs.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable, Sequence
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
from typing import Protocol

from .config import NotifyChannel, Settings
from .models import Lead

logger = logging.getLogger(__name__)

# Port 465 is implicit TLS; everything else negotiates STARTTLS.
_IMPLICIT_TLS_PORT = 465

# Brand palette for the HTML email. Best-effort match (see module docstring).
_BRAND_BLACK = "#0a0a0a"
_BRAND_YELLOW = "#FFD400"
_BRAND_YELLOW_BADGE_BG = "#FFF6CC"
_BRAND_YELLOW_BADGE_TEXT = "#7A5B00"
_FONT_STACK = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


class Notifier(Protocol):
    """Anything that can announce a batch of leads."""

    def send(self, leads: Sequence[Lead]) -> None:
        """Deliver a notification, or raise if it could not be delivered."""
        ...


class CompositeNotifyError(RuntimeError):
    """One or more channels in a CompositeNotifier failed to send."""


class CompositeNotifier:
    """Fans a batch out to every channel NOTIFY_CHANNEL names.

    Every notifier is attempted, even if an earlier one raised — a failing
    email must not suppress a WhatsApp message that would otherwise have
    gone through, and vice versa; whichever channel still works should still
    reach someone. If any of them failed, the whole call still raises once
    every notifier has had its turn, so the caller (``monitor.run_once``)
    never marks the batch notified. The next run then retries *every*
    channel, including whichever one already succeeded, which can duplicate
    a message on the channel that worked. Accepted for the same reason a
    duplicate is already accepted within a single channel's multiple
    recipients (see ``whatsapp_notifier.py``): a repeat is an annoyance, a
    channel that silently never got its half of the alert is not.
    """

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = notifiers

    def send(self, leads: Sequence[Lead]) -> None:
        failures: list[str] = []
        for notifier in self._notifiers:
            try:
                notifier.send(leads)
            except Exception as error:
                name = type(notifier).__name__
                logger.exception("A channel failed to send", extra={"channel": name})
                failures.append(f"{name}: {error}")

        if failures:
            raise CompositeNotifyError(
                f"{len(failures)}/{len(self._notifiers)} channel(s) failed: {'; '.join(failures)}"
            )


def build_channel_notifier(settings: Settings, channel: NotifyChannel) -> Notifier:
    """Instantiate one specific channel's notifier, regardless of how many
    channels NOTIFY_CHANNEL lists. Exposed (not just used by build_notifier
    below) so callers like ``cli.cmd_test_notify`` can exercise one channel
    at a time even when more than one is configured."""
    if channel is NotifyChannel.WHATSAPP:
        from .whatsapp_notifier import WhatsAppNotifier

        return WhatsAppNotifier(settings)
    if channel is NotifyChannel.CALLMEBOT:
        from .callmebot_notifier import CallMeBotNotifier

        return CallMeBotNotifier(settings)
    return SmtpNotifier(settings)


def build_notifier(settings: Settings) -> Notifier:
    """Instantiate the notifier(s) ``NOTIFY_CHANNEL`` asks for.

    A single channel returns that channel's notifier directly, unchanged
    from before more than one channel was possible. More than one channel
    returns a ``CompositeNotifier`` that fires all of them per batch.
    """
    notifiers = [build_channel_notifier(settings, channel) for channel in settings.notify_channels]
    if len(notifiers) == 1:
        return notifiers[0]
    return CompositeNotifier(notifiers)


class SmtpNotifier:
    """Sends a single multipart message over SMTP.

    ``transport_factory`` exists so tests can substitute a fake without opening
    a socket; production passes nothing and gets a real :mod:`smtplib` client.
    """

    def __init__(
        self,
        settings: Settings,
        transport_factory: Callable[[], smtplib.SMTP] | None = None,
    ) -> None:
        self._settings = settings
        self._transport_factory = transport_factory or self._default_transport

    def _default_transport(self) -> smtplib.SMTP:
        settings = self._settings
        timeout = settings.request_timeout_seconds
        context = ssl.create_default_context()

        if settings.smtp_port == _IMPLICIT_TLS_PORT:
            return smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=timeout, context=context
            )

        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        return client

    def send(self, leads: Sequence[Lead]) -> None:
        if not leads:
            logger.debug("Nothing to notify")
            return

        message = self.build_message(leads)
        settings = self._settings

        with self._transport_factory() as transport:
            transport.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            transport.send_message(message)

        logger.info(
            "Notification sent",
            extra={"leads": len(leads), "recipients": len(settings.recipients)},
        )

    # -- message construction ---------------------------------------------

    def build_message(self, leads: Sequence[Lead]) -> EmailMessage:
        settings = self._settings

        message = EmailMessage()
        message["Subject"] = self._subject(leads)
        message["From"] = formataddr((self._sender_name(), settings.mail_from))
        message["To"] = ", ".join(settings.recipients)
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="lead-monitor.local")
        # Marks the mail as automated so recipients' out-of-office replies and
        # vacation responders stay quiet.
        message["Auto-Submitted"] = "auto-generated"

        message.set_content(self._plain_body(leads))
        message.add_alternative(self._html_body(leads), subtype="html")
        return message

    @property
    def _redact(self) -> bool:
        """Whether to keep personal data out of the email. Default: yes."""
        return not self._settings.notify_include_personal_data

    def _sender_name(self) -> str:
        center = self._settings.center_name
        return f"Brooklyn Lead Monitor — {center}" if center else "Brooklyn Lead Monitor"

    def _subject_prefix(self) -> str:
        """CENTER_NAME as a subject prefix, so a shared inbox can tell
        deployments apart without opening the message. Empty when unset —
        single-center use looks exactly as it did before this existed."""
        center = self._settings.center_name
        return f"[{center}] " if center else ""

    def _subject(self, leads: Sequence[Lead]) -> str:
        prefix = self._subject_prefix()
        if len(leads) != 1:
            return f"{prefix}{len(leads)} leads nuevos en pre-order"
        # Even the subject stays free of personal data by default; the arrival
        # time identifies the alert without naming the person.
        if self._redact:
            return f"{prefix}Lead nuevo en pre-order — {self._format_datetime(leads[0].created_at)}"
        return f"{prefix}Lead nuevo en pre-order: {leads[0].summary()}"

    @staticmethod
    def _format_datetime(value: datetime | None) -> str:
        # Seconds included on purpose: the request is the *exact* arrival time.
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else "hora desconocida"

    # -- shared HTML chrome --------------------------------------------------

    def _html_header(self) -> str:
        center = self._settings.center_name
        subtitle = f"Lead Monitor · {escape(center)}" if center else "Lead Monitor"
        return f"""\
      <tr>
        <td style="background:{_BRAND_BLACK};padding:28px 32px;\
border-bottom:4px solid {_BRAND_YELLOW};">
          <div style="font-family:{_FONT_STACK};font-size:19px;font-weight:800;\
letter-spacing:0.5px;color:#ffffff;">
            BROOKLYN <span style="color:{_BRAND_YELLOW};">FITBOXING</span>
          </div>
          <div style="font-family:{_FONT_STACK};font-size:12px;font-weight:600;\
letter-spacing:1.5px;color:{_BRAND_YELLOW};text-transform:uppercase;margin-top:4px;">
            {subtitle}
          </div>
        </td>
      </tr>"""

    @staticmethod
    def _html_footer() -> str:
        return f"""\
      <tr>
        <td style="padding:18px 32px;background:#fafafa;border-top:1px solid #ececec;">
          <div style="font-family:{_FONT_STACK};font-size:11px;color:#9a9a9a;line-height:1.5;">
            Brooklyn Lead Monitor · mensaje automático, no respondas a este correo.
          </div>
        </td>
      </tr>"""

    @staticmethod
    def _pre_order_badge() -> str:
        return f"""\
            <span style="display:inline-block;background:{_BRAND_YELLOW_BADGE_BG};\
color:{_BRAND_YELLOW_BADGE_TEXT};font-size:11px;font-weight:700;letter-spacing:0.5px;\
text-transform:uppercase;padding:4px 10px;border-radius:20px;font-family:{_FONT_STACK};\
margin-right:6px;">
              Pre Order
            </span>"""

    @staticmethod
    def _center_badge(center: str) -> str:
        """A high-contrast pill naming the center, sitting right next to the
        Pre Order badge in the body — not just the small header subtitle —
        so it's the first thing visible on opening the email, not something
        you have to notice in secondary text. See CENTER_NAME."""
        return f"""\
            <span style="display:inline-block;background:{_BRAND_BLACK};\
color:{_BRAND_YELLOW};font-size:11px;font-weight:700;letter-spacing:0.5px;\
text-transform:uppercase;padding:4px 10px;border-radius:20px;font-family:{_FONT_STACK};\
margin-right:6px;">
              {escape(center)}
            </span>"""

    def _badges_row(self) -> str:
        center = self._settings.center_name
        badges = self._pre_order_badge()
        if center:
            badges = self._center_badge(center) + "\n" + badges
        return badges

    # -- redacted bodies (default) ----------------------------------------

    def _center_line(self) -> list[str]:
        """A "Centro: X" line, or nothing when CENTER_NAME is unset."""
        center = self._settings.center_name
        return [f"Centro: {center}", ""] if center else []

    def _plain_body(self, leads: Sequence[Lead]) -> str:
        if not self._redact:
            return self._plain_body_full(leads)

        count = len(leads)
        noun = "lead" if count == 1 else "leads"
        lines = [
            *self._center_line(),
            f"{count} {noun} nuevo{'s' if count != 1 else ''} en pre-order.",
            "",
            "Los datos de contacto se mantienen en el portal, no en este correo. "
            "Ábrelo para ver quiénes son:",
            self._settings.leads_page_url(),
            "",
            "Horas de llegada:",
        ]
        for lead in leads:
            lines.append(f"  • {self._format_datetime(lead.created_at)}   (ref {lead.external_id})")
        return "\n".join(lines)

    def _html_body(self, leads: Sequence[Lead]) -> str:
        if not self._redact:
            return self._html_body_full(leads)

        count = len(leads)
        heading = (
            "1 lead nuevo en pre-order" if count == 1 else f"{count} leads nuevos en pre-order"
        )
        subheading = "Los datos de contacto se mantienen en el portal, no en este correo."
        rows = "\n".join(
            f"""      <tr>
        <td style="padding:12px 16px;font-size:14px;color:#222222;\
font-variant-numeric:tabular-nums;border-bottom:1px solid #f2f2f2;">\
{escape(self._format_datetime(lead.created_at))}</td>
        <td style="padding:12px 16px;font-size:14px;color:#8a8a8a;\
border-bottom:1px solid #f2f2f2;">ref&nbsp;{escape(lead.external_id)}</td>
      </tr>"""
            for lead in leads
        )
        table = f"""\
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="border:1px solid #ececec;border-radius:8px;overflow:hidden;font-family:{_FONT_STACK};">
              <tr style="background:#fafafa;">
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Llegada</td>
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Referencia</td>
              </tr>
{rows}
            </table>"""

        return self._render_shell(heading, subheading, table)

    # -- full bodies (opt-in) ---------------------------------------------

    def _plain_body_full(self, leads: Sequence[Lead]) -> str:
        lines = [*self._center_line(), f"{len(leads)} lead(s) nuevo(s) en pre-order.", ""]
        for lead in leads:
            lines.extend(
                [
                    f"• {lead.name or '(sin nombre)'}",
                    f"    Email:    {lead.email or '—'}",
                    f"    Teléfono: {lead.phone or '—'}",
                    f"    Club:     {lead.club or '—'}",
                    f"    Estado:   {lead.status or '—'}",
                    f"    Llegada:  {self._format_datetime(lead.created_at)}",
                    f"    Ref:      {lead.external_id}",
                    "",
                ]
            )
        lines.append(self._settings.leads_page_url())
        return "\n".join(lines)

    def _html_body_full(self, leads: Sequence[Lead]) -> str:
        heading = (
            "1 lead nuevo en pre-order"
            if len(leads) == 1
            else (f"{len(leads)} leads nuevos en pre-order")
        )
        subheading = "Datos completos, porque NOTIFY_INCLUDE_PERSONAL_DATA está activado."
        rows = "\n".join(
            f"""      <tr>
        <td style="padding:10px 16px;font-size:14px;color:#222222;\
border-bottom:1px solid #f2f2f2;">{escape(lead.name) or "—"}</td>
        <td style="padding:10px 16px;font-size:14px;color:#222222;\
border-bottom:1px solid #f2f2f2;">{escape(lead.email) or "—"}</td>
        <td style="padding:10px 16px;font-size:14px;color:#222222;\
border-bottom:1px solid #f2f2f2;">{escape(lead.phone) or "—"}</td>
        <td style="padding:10px 16px;font-size:14px;color:#222222;\
font-variant-numeric:tabular-nums;border-bottom:1px solid #f2f2f2;">\
{escape(self._format_datetime(lead.created_at))}</td>
      </tr>"""
            for lead in leads
        )
        table = f"""\
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="border:1px solid #ececec;border-radius:8px;overflow:hidden;font-family:{_FONT_STACK};">
              <tr style="background:#fafafa;">
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Nombre</td>
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Email</td>
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Teléfono</td>
                <td style="padding:10px 16px;font-size:11px;font-weight:700;\
letter-spacing:0.5px;text-transform:uppercase;color:#8a8a8a;border-bottom:1px solid #ececec;">\
Llegada</td>
              </tr>
{rows}
            </table>"""

        return self._render_shell(heading, subheading, table)

    # -- shell rendering (bound to the real portal URL) --------------------

    def _render_shell(self, heading: str, subheading: str, table: str) -> str:
        portal = escape(self._settings.leads_page_url())
        return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:24px 12px;background:#e9e9e9;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:10px;\
overflow:hidden;font-family:{_FONT_STACK};">
{self._html_header()}
      <tr>
        <td style="padding:32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr><td style="padding-bottom:6px;">{self._badges_row()}</td></tr>
            <tr>
              <td style="font-family:{_FONT_STACK};font-size:21px;font-weight:800;\
color:#111111;padding:8px 0 4px;">
                {heading}
              </td>
            </tr>
            <tr>
              <td style="font-family:{_FONT_STACK};font-size:13px;color:#6b6b6b;\
line-height:1.5;padding-bottom:20px;">
                {subheading}
              </td>
            </tr>
            <tr><td>{table}</td></tr>
            <tr>
              <td style="padding-top:26px;">
                <table role="presentation" cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="border-radius:8px;background:{_BRAND_YELLOW};">
                      <a href="{portal}" style="display:inline-block;padding:12px 22px;\
font-family:{_FONT_STACK};font-size:14px;font-weight:800;color:{_BRAND_BLACK};\
text-decoration:none;">
                        Abrir el portal&nbsp;→
                      </a>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
{self._html_footer()}
    </table>
  </body>
</html>"""
