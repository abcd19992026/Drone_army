"""tests/test_alerts.py -- Phase 11 verification suite.

Tests gss/alerts.py (WhatsApp SOS alerts) and its wiring into gss/commands.py
in isolation: a MagicMock stands in for TelemetryStore, so none of this needs
a real Supabase, a MAVLink link, or the (not-yet-wired) WhatsApp Business
API. Mirrors tests/test_scheduler.py's Phase 9 pytest style.

Verification items (per the Phase 11 spec):
  1  Active contacts exist, WhatsApp API call succeeds -> alerts row written
     with status='sent', contacts_notified lists them all.
  2  No active contacts -> alerts row written with status='failed',
     descriptive error_detail, no API call attempted, no exception raised.
  3  WhatsApp API call raises/times out -> alerts row written with
     status='failed', exception caught, send() does not raise.
  4  Some recipients succeed, some fail -> status='partial', contacts_notified
     reflects which.
  5  An "sos" command still creates a 'family_summon' mission exactly like an
     ordinary flight command, independent of alert outcome -- both directions
     (alert-sending raises -> mission still created; mission creation fails
     -> alert still sends).
  6  store.get_active_contacts() unreachable -> treated like "no active
     contacts" (R10: log, write failed alert row, no crash).
  8  ALERTS_ENABLED=false -> commands.py never imports/calls gss.alerts; an
     "sos" command still flies (mission creation unaffected).

(Item 7 -- "sos" is a KNOWN + FLIGHT command type mapped to mission type
'family_summon' -- is a single assertion added to tests/test_commands.py's
live-Supabase suite, not here.)
"""

from __future__ import annotations

import inspect
import json
import os
import uuid
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("WEATHER_ENABLED", "false")
os.environ.setdefault("MEDIA_ENABLED", "false")

from gss import config
from gss.alerts import AlertSender, build_alert_payload
from gss.commands import CommandIntake


def _cid() -> str:
    return str(uuid.uuid4())


def _contact(*, contact_id=None, name="Alice", phone="+15550001111", priority=100):
    return {
        "id": contact_id or _cid(), "name": name, "phone": phone,
        "relation": "wife", "priority": priority, "active": True,
    }


# ── pure core: build_alert_payload ──────────────────────────────────────────

def test_pure_build_alert_payload_shapes_recipients_and_drops_phoneless():
    contacts = [
        _contact(contact_id="c1", name="Alice", phone="+1111"),
        {"id": "c2", "name": "No Phone", "phone": None, "priority": 50, "active": True},
    ]
    payload = build_alert_payload(contacts, "http://status.example/m1")

    assert payload["recipients"] == [{"contact_id": "c1", "name": "Alice", "phone": "+1111"}]
    assert payload["template_variables"] == {"status_page_url": "http://status.example/m1"}


# ── 1: active contacts, API succeeds -> 'sent' ──────────────────────────────

def test_1_active_contacts_success_writes_sent():
    store = MagicMock()
    store.get_active_contacts.return_value = [
        _contact(contact_id="c1", priority=200), _contact(contact_id="c2", priority=100),
    ]
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one", return_value=(True, "delivered")):
        sender.send("mission-1")

    store.create_alert.assert_called_once()
    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["mission_id"] == "mission-1"
    assert kwargs["channel"] == "whatsapp"
    assert kwargs["status"] == "sent"
    assert len(kwargs["contacts_notified"]) == 2
    assert all(c["delivered"] for c in kwargs["contacts_notified"])
    assert kwargs["error_detail"] is None


# ── 2: no active contacts -> 'failed', no API call, no exception ───────────

def test_2_no_active_contacts_writes_failed_no_api_call():
    store = MagicMock()
    store.get_active_contacts.return_value = []
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one") as send_one:
        sender.send("mission-2")  # must not raise

    send_one.assert_not_called()
    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["mission_id"] == "mission-2"
    assert "no active contacts" in kwargs["error_detail"]


# ── 3: API call raises/times out -> 'failed', caught, no exception ─────────

def test_3_api_raises_writes_failed_no_exception():
    store = MagicMock()
    store.get_active_contacts.return_value = [_contact()]
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one", side_effect=TimeoutError("no response")):
        sender.send("mission-3")  # must not raise

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert "TimeoutError" in kwargs["error_detail"]


# ── 4: partial delivery -> 'partial', contacts_notified reflects which ─────

def test_4_partial_delivery_writes_partial():
    store = MagicMock()
    store.get_active_contacts.return_value = [
        _contact(contact_id="c1", name="Alice", phone="+1111", priority=200),
        _contact(contact_id="c2", name="Bob", phone="+2222", priority=100),
    ]
    sender = AlertSender(store)

    def fake_send_one(_self, recipient, _template_variables):
        if recipient["phone"] == "+1111":
            return True, "delivered"
        return False, "recipient number not on WhatsApp"

    with patch.object(AlertSender, "_send_one", fake_send_one):
        sender.send("mission-4")

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "partial"
    delivered = {c["phone"]: c["delivered"] for c in kwargs["contacts_notified"]}
    assert delivered == {"+1111": True, "+2222": False}
    assert "Bob" in kwargs["error_detail"]


# ── 6: store.get_active_contacts() unreachable -> treated as no contacts ───

def test_6_contacts_unreachable_treated_as_no_contacts():
    # R10: store.py's own retry-safe read (get_active_contacts) already logs
    # why and returns [] on an unreachable Supabase -- indistinguishable here
    # from "no contacts configured", and AlertSender must handle both the
    # same way: no crash, a failed alerts row, no API call.
    store = MagicMock()
    store.get_active_contacts.return_value = []
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one") as send_one:
        sender.send("mission-6")  # must not raise

    send_one.assert_not_called()
    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"


def test_send_never_raises_even_on_a_totally_broken_store():
    store = MagicMock()
    store.get_active_contacts.side_effect = RuntimeError("boom")
    sender = AlertSender(store)

    sender.send("mission-x")  # must not raise

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"


# ── _send_one: the real Meta Cloud API call, guarded on missing config ─────

def test_send_one_refuses_without_full_config(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "")  # not set
    sender = AlertSender(MagicMock(), template_name=None)  # no template either

    ok, detail = sender._send_one({"phone": "+1111"}, {"status_page_url": "http://x/m1"})

    assert ok is False
    assert "WABA_ACCESS_TOKEN" in detail
    assert "WABA_TEMPLATE_NAME" in detail


def test_send_one_posts_to_graph_api_when_fully_configured(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="incident_alert")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = b"{}"
    with patch("gss.alerts.urllib.request.urlopen", return_value=fake_resp) as urlopen:
        ok, detail = sender._send_one({"phone": "+1111"}, {"status_page_url": "http://x/m1"})

    assert ok is True
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://graph.facebook.com/v21.0/123/messages"
    assert request.get_header("Authorization") == "Bearer secret-token"
    body = json.loads(request.data)
    assert body["to"] == "+1111"
    assert body["template"]["name"] == "incident_alert"
    assert body["template"]["components"][0]["parameters"][0]["text"] == "http://x/m1"


def test_send_one_returns_failure_on_http_error(monkeypatch):
    import urllib.error

    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="incident_alert")

    exc = urllib.error.HTTPError(
        "http://x", 401, "Unauthorized", None, MagicMock(read=lambda: b"bad token")
    )
    with patch("gss.alerts.urllib.request.urlopen", side_effect=exc):
        ok, detail = sender._send_one({"phone": "+1111"}, {"status_page_url": "http://x/m1"})

    assert ok is False
    assert "401" in detail


# ── 5: alert + flight dispatch are independent, both directions ────────────

def _sos_cmd(command_id=None):
    return {
        "id": command_id or _cid(), "type": "sos",
        "target_lat": 25.60, "target_lon": 85.21, "target_alt_m": 30.0,
        "issued_by": "phone", "params": {},
    }


def test_5a_alert_send_raises_mission_creation_still_happens(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    store = MagicMock()
    store.create_mission.return_value = "mission-5a"
    intake = CommandIntake(store, lambda: MagicMock(), realtime_enabled=False)
    cmd = _sos_cmd()

    with patch.object(intake, "_start_executor") as start_executor, \
         patch("gss.alerts.AlertSender") as sender_cls:
        sender_cls.return_value.send.side_effect = RuntimeError("whatsapp down")
        intake._accept_flight(cmd)  # must not raise

    store.create_mission.assert_called_once()
    store.link_command_to_mission.assert_called_once_with(cmd["id"], "mission-5a")
    start_executor.assert_called_once()
    sender_cls.return_value.send.assert_called_once_with("mission-5a")


def test_5b_mission_creation_fails_alert_still_sends(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    store = MagicMock()
    store.create_mission.return_value = None  # simulated failure
    intake = CommandIntake(store, lambda: MagicMock(), realtime_enabled=False)
    cmd = _sos_cmd()

    with patch("gss.alerts.AlertSender") as sender_cls:
        intake._accept_flight(cmd)  # must not raise

    sender_cls.return_value.send.assert_called_once_with(None)
    # the command is rejected because of the mission failure, independent of
    # the alert outcome
    store.update_command_status.assert_called_once_with(
        cmd["id"], "rejected",
        rejected_reason="internal error: could not create the mission",
    )


# ── 8: ALERTS_ENABLED=false -> gss.alerts never imported/called ────────────

def test_8a_alerts_disabled_guard_precedes_the_import():
    """Structural check (mirrors tests/test_scheduler.py's test_10): the
    ALERTS_ENABLED early-return must appear before the `from gss.alerts
    import` line in the source, so a disabled alert channel can never reach
    the import -- not just "happens not to call send()"."""
    src = inspect.getsource(CommandIntake._fire_sos_alert)
    guard_pos = src.index("config.ALERTS_ENABLED")
    import_pos = src.index("from gss.alerts import")
    assert guard_pos < import_pos


def test_8b_alerts_disabled_never_calls_create_alert_mission_unaffected(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", False)
    store = MagicMock()
    store.create_mission.return_value = "mission-8"
    intake = CommandIntake(store, lambda: MagicMock(), realtime_enabled=False)
    cmd = _sos_cmd()

    with patch.object(intake, "_start_executor") as start_executor:
        intake._accept_flight(cmd)

    store.create_alert.assert_not_called()
    store.create_mission.assert_called_once()
    start_executor.assert_called_once()
