"""gss/alerts.py -- WhatsApp SOS alerts (Phase 11).

PROJECT.md Section 13: "in the first ~90 seconds of an incident, the
phone/software does the real work" -- this module is that work's WhatsApp
half. The other half is drone dispatch, and the two are DELIBERATELY
UNLINKED: a geofence rejection, a dead battery, bad weather -- none of that
may ever stop this alert from going out, and conversely a dead WhatsApp API
must never stop or delay the drone. gss/commands.py's ``_fire_sos_alert``
fires this from its own try/except, right alongside (not nested inside) the
flight-mission code path, and neither call's outcome affects the other.

Mirrors the pure/shell split used by weather.py and scheduler.py:

  * :func:`build_alert_payload` -- pure. Active contacts (already sorted by
    priority) + a status-page URL -> the WhatsApp template payload
    structure. No network, no I/O.
  * :class:`AlertSender` -- the networked shell. Unlike
    :class:`~gss.weather_feed.WeatherMonitor` / :class:`~gss.scheduler.PatrolScheduler`
    this is NOT a background-thread monitor: an SOS alert fires once per SOS
    command, so this is a one-shot action with a single ``send()`` call, no
    ``start()``/``close()``.

R10 holds here exactly as it does in store.py: a slow or failed WhatsApp call
must never raise out of :meth:`AlertSender.send`, block the caller beyond an
ordinary per-request HTTP timeout, or leave the incident unrecorded. Every
path through ``send()`` ends with exactly one ``alerts`` row written, whether
that is 'sent', 'partial', or 'failed'.

CONFIG NOTE: the ``sos_alert`` template (UTILITY category, no CTA -- a CTA
gets a template reclassified as Marketing at ~8x cost, PROJECT.md Section 13)
is now APPROVED on Meta, and :meth:`AlertSender._send_one` makes the real
Meta Cloud API call: two body variables, {{1}} the name of the person the
alert is about (``config.SOS_PERSON_NAME`` -- this is a single-operator
system, so it is one fixed name, not per-mission data) and {{2}} the live
status page link. It still REFUSES to call the API (writing a clear 'failed'
alerts row instead) if ``WABA_PHONE_NUMBER_ID``, ``WABA_ACCESS_TOKEN`` or
``WABA_TEMPLATE_NAME`` is ever unset -- an unconfigured alert channel must
stay visible, never silently pretend to succeed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from gss import config

log = logging.getLogger(__name__)


# ===========================================================================
# pure core
# ===========================================================================


def build_alert_payload(contacts: list[dict], status_page_url: str, person_name: str) -> dict:
    """Pure. Active contacts (already sorted by priority, highest first --
    :meth:`~gss.store.TelemetryStore.get_active_contacts`'s own ordering), the
    live-status-page URL, and the name of the person the alert is about ->
    the WhatsApp template payload: one recipient per contact with a phone
    number, plus the template variables every recipient's message shares. No
    network, no I/O.

    Contacts with no phone number are silently dropped here rather than
    failed later against the WhatsApp API -- there is nothing to send them.
    """
    recipients = [
        {"contact_id": c.get("id"), "name": c.get("name"), "phone": c.get("phone")}
        for c in contacts
        if c.get("phone")
    ]
    return {
        "recipients": recipients,
        "template_variables": {
            "person_name": person_name,
            "status_page_url": status_page_url,
        },
    }


# ===========================================================================
# the shell
# ===========================================================================


class AlertSender:
    """Fires the WhatsApp SOS alert once and writes the ``alerts`` row
    recording what happened.

    A one-shot action, not a monitor thread: built and ``send()``-ed once per
    SOS command by :meth:`gss.commands.CommandIntake._fire_sos_alert`,
    completely independent of that same command's flight dispatch.
    """

    def __init__(
        self, store, *, template_name: str | None = None, template_lang: str | None = None
    ) -> None:
        self._store = store
        self._template_name = template_name or config.WABA_TEMPLATE_NAME or None
        self._template_lang = template_lang or config.WABA_TEMPLATE_LANG or "en"

    def send(self, mission_id: str | None) -> None:
        """Send the SOS WhatsApp alert for ``mission_id`` (may be ``None`` if
        mission creation failed -- the alert still goes out) and write the
        resulting ``alerts`` row. Never raises."""
        try:
            self._send(mission_id)
        except Exception:
            # Every expected failure mode (no contacts, a per-recipient
            # exception, an unreachable store) is already handled inside
            # _send without raising. Reaching here means something in this
            # method itself is broken -- still must not crash the caller,
            # and the incident still must not go unrecorded.
            log.exception(
                "alerts: send(mission_id=%s) hit an unexpected internal error "
                "-- writing a failed alerts row so the failure is not silent",
                mission_id,
            )
            self._write_alert(
                mission_id, status="failed", contacts_notified=None,
                error_detail="internal error in AlertSender.send() -- see the GSS log",
            )

    def _send(self, mission_id: str | None) -> None:
        contacts = self._store.get_active_contacts()
        if not contacts:
            # R10: an unreachable store already logged why and returned an
            # empty list (store.py's own retry-safe pattern) -- indistin-
            # guishable here from "no contacts configured", and both are
            # "nothing to send", never a crash.
            log.warning(
                "sos: no active contacts configured -- mission %s, nobody notified",
                mission_id,
            )
            self._write_alert(
                mission_id, status="failed", contacts_notified=[],
                error_detail="no active contacts configured",
            )
            return

        status_page_url = f"{config.STATUS_PAGE_BASE_URL}/{mission_id}"
        payload = build_alert_payload(contacts, status_page_url, config.SOS_PERSON_NAME)

        notified: list[dict] = []
        failed: list[dict] = []
        for recipient in payload["recipients"]:
            try:
                ok, detail = self._send_one(recipient, payload["template_variables"])
            except Exception as exc:  # noqa: BLE001 -- a dead API must never propagate
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            (notified if ok else failed).append({**recipient, "delivered": ok, "detail": detail})

        if not notified:
            status = "failed"
        elif failed:
            status = "partial"
        else:
            status = "sent"

        error_detail = None
        if failed:
            error_detail = "; ".join(
                f"{r.get('name') or r.get('phone')}: {r['detail']}" for r in failed
            )

        self._write_alert(
            mission_id, status=status,
            contacts_notified=notified + failed,
            error_detail=error_detail,
        )
        log.log(
            logging.INFO if status == "sent" else logging.WARNING,
            "sos: WhatsApp alert %s -- mission %s, %d/%d recipient(s) notified",
            status, mission_id, len(notified), len(notified) + len(failed),
        )

    def _send_one(self, recipient: dict, template_variables: dict) -> tuple[bool, str]:
        """POST one WhatsApp template message via the Meta WhatsApp Business
        Cloud API. Returns ``(ok, detail)`` -- never raises (the per-recipient
        caller in :meth:`_send` also catches, but network/HTTP errors are
        handled here directly so ``detail`` stays a useful message rather
        than a bare exception repr).

        Refuses to call the API at all -- and returns a clear failure
        instead -- unless ``WABA_PHONE_NUMBER_ID``, ``WABA_ACCESS_TOKEN`` and
        ``WABA_TEMPLATE_NAME`` are ALL configured: an unconfigured alert
        channel must be visible (a 'failed' alerts row with a clear
        error_detail), never silently pretend to succeed or send a request
        built around a guessed template.

        The ``sos_alert`` template (approved 2026-09) takes two body
        variables in order: {{1}} the name of the person the alert is about,
        {{2}} the live status page link.
        """
        missing = [
            name for name, value in (
                ("WABA_PHONE_NUMBER_ID", config.WABA_PHONE_NUMBER_ID),
                ("WABA_ACCESS_TOKEN", config.WABA_ACCESS_TOKEN),
                ("WABA_TEMPLATE_NAME", self._template_name),
            )
            if not value
        ]
        if missing:
            return False, f"WhatsApp not configured yet ({', '.join(missing)} unset)"

        url = f"https://graph.facebook.com/v25.0/{config.WABA_PHONE_NUMBER_ID}/messages"
        body = {
            "messaging_product": "whatsapp",
            "to": recipient.get("phone"),
            "type": "template",
            "template": {
                "name": self._template_name,
                "language": {"code": self._template_lang},
                "components": [{
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": template_variables["person_name"]},
                        {"type": "text", "text": template_variables["status_page_url"]},
                    ],
                }],
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {config.WABA_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                resp_body = resp.read()
            message_id = None
            try:
                message_id = json.loads(resp_body)["messages"][0]["id"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                pass
            return True, message_id or "sent"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            log.warning(
                "sos: WhatsApp send to %s failed: HTTP %d: %s",
                recipient.get("phone"), exc.code, detail,
            )
            return False, f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning(
                "sos: WhatsApp send to %s failed: %s: %s",
                recipient.get("phone"), type(exc).__name__, exc,
            )
            return False, f"{type(exc).__name__}: {exc}"

    def _write_alert(
        self,
        mission_id: str | None,
        *,
        status: str,
        contacts_notified: list | None,
        error_detail: str | None,
    ) -> None:
        self._store.create_alert(
            mission_id=mission_id,
            channel="whatsapp",
            status=status,
            template_name=self._template_name,
            contacts_notified=contacts_notified,
            error_detail=error_detail,
        )
