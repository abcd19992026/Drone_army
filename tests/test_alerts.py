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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("WEATHER_ENABLED", "false")
os.environ.setdefault("MEDIA_ENABLED", "false")

from gss import config
from gss.alerts import AlertSender, build_alert_payload
from gss.commands import CommandIntake, _Box
from gss.safety import SafetyAction


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
    payload = build_alert_payload(contacts, "http://status.example/m1", "Raj")

    assert payload["recipients"] == [{"contact_id": "c1", "name": "Alice", "phone": "+1111"}]
    assert payload["template_variables"] == {
        "person_name": "Raj",
        "status_page_url": "http://status.example/m1",
    }


# ── 1: active contacts, API succeeds -> 'sent' ──────────────────────────────

def test_1_active_contacts_success_writes_sent():
    store = MagicMock()
    store.get_active_contacts.return_value = [
        _contact(contact_id="c1", priority=200), _contact(contact_id="c2", priority=100),
    ]
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one", return_value=(True, "delivered")):
        sender.send("cmd-1")

    store.create_alert.assert_called_once()
    kwargs = store.create_alert.call_args.kwargs
    # mission_id is always None -- no mission exists at send-time (see
    # AlertSender.send's docstring); the command id is carried in `detail`
    # instead, since alerts.mission_id has an FK to missions(id).
    assert kwargs["mission_id"] is None
    assert kwargs["detail"] == {"command_id": "cmd-1"}
    assert kwargs["channel"] == "whatsapp"
    assert kwargs["status"] == "sent"
    assert len(kwargs["contacts_notified"]) == 2
    assert all(c["delivered"] for c in kwargs["contacts_notified"])
    assert kwargs["error_detail"] is None


def test_url_construction_uses_command_id_not_mission_id(monkeypatch):
    monkeypatch.setattr(config, "STATUS_PAGE_BASE_URL", "http://status.example")
    store = MagicMock()
    store.get_active_contacts.return_value = [_contact()]
    sender = AlertSender(store)

    captured = {}

    def fake_send_one(_self, _recipient, template_variables):
        captured["status_page_url"] = template_variables["status_page_url"]
        return True, "sent"

    with patch.object(AlertSender, "_send_one", fake_send_one):
        sender.send("cmd-xyz")

    assert captured["status_page_url"] == "http://status.example/cmd-xyz"


# ── 2: no active contacts -> 'failed', no API call, no exception ───────────

def test_2_no_active_contacts_writes_failed_no_api_call():
    store = MagicMock()
    store.get_active_contacts.return_value = []
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one") as send_one:
        sender.send("cmd-2")  # must not raise

    send_one.assert_not_called()
    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["mission_id"] is None
    assert kwargs["detail"] == {"command_id": "cmd-2"}
    assert "no active contacts" in kwargs["error_detail"]


# ── 3: API call raises/times out -> 'failed', caught, no exception ─────────

def test_3_api_raises_writes_failed_no_exception():
    store = MagicMock()
    store.get_active_contacts.return_value = [_contact()]
    sender = AlertSender(store)

    with patch.object(AlertSender, "_send_one", side_effect=TimeoutError("no response")):
        sender.send("cmd-3")  # must not raise

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
        sender.send("cmd-4")

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "partial"
    delivered = {c["phone"]: c["delivered"] for c in kwargs["contacts_notified"]}
    assert delivered == {"+1111": True, "+2222": False}
    assert "Bob" in kwargs["error_detail"]


# ── end-to-end: real _send_one wired through _send, person_name from config ─

def test_end_to_end_sent_uses_configured_person_name_and_records_message_id(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    monkeypatch.setattr(config, "SOS_PERSON_NAME", "Raj")
    monkeypatch.setattr(config, "STATUS_PAGE_BASE_URL", "http://status.example")

    store = MagicMock()
    store.get_active_contacts.return_value = [_contact(contact_id="c1", phone="+1111")]
    sender = AlertSender(store, template_name="sos_alert")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = json.dumps(
        {"messages": [{"id": "wamid.END2END"}]}
    ).encode("utf-8")
    with patch("gss.alerts.urllib.request.urlopen", return_value=fake_resp) as urlopen:
        sender.send("cmd-e2e")

    body = json.loads(urlopen.call_args.args[0].data)
    params = body["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "Raj"
    assert params[1]["text"] == "http://status.example/cmd-e2e"

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "sent"
    assert kwargs["contacts_notified"][0]["delivered"] is True
    assert kwargs["contacts_notified"][0]["detail"] == "wamid.END2END"


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
        sender.send("cmd-6")  # must not raise

    send_one.assert_not_called()
    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"


def test_send_never_raises_even_on_a_totally_broken_store():
    store = MagicMock()
    store.get_active_contacts.side_effect = RuntimeError("boom")
    sender = AlertSender(store)

    sender.send("cmd-x")  # must not raise

    kwargs = store.create_alert.call_args.kwargs
    assert kwargs["status"] == "failed"


# ── _send_one: the real Meta Cloud API call, guarded on missing config ─────

def test_send_one_refuses_without_full_config(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "")  # not set
    monkeypatch.setattr(config, "WABA_TEMPLATE_NAME", "")  # not set either
    sender = AlertSender(MagicMock(), template_name=None)  # no override

    ok, detail = sender._send_one(
        {"phone": "+1111"}, {"person_name": "Raj", "status_page_url": "http://x/m1"}
    )

    assert ok is False
    assert "WABA_ACCESS_TOKEN" in detail
    assert "WABA_TEMPLATE_NAME" in detail


def test_send_one_posts_to_graph_api_when_fully_configured(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="sos_alert", template_lang="en")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = json.dumps(
        {"messages": [{"id": "wamid.HBgABC123"}]}
    ).encode("utf-8")
    with patch("gss.alerts.urllib.request.urlopen", return_value=fake_resp) as urlopen:
        ok, detail = sender._send_one(
            {"phone": "+1111"}, {"person_name": "Raj", "status_page_url": "http://x/m1"}
        )

    assert ok is True
    assert detail == "wamid.HBgABC123"
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://graph.facebook.com/v25.0/123/messages"
    assert request.get_header("Authorization") == "Bearer secret-token"
    body = json.loads(request.data)
    assert body["to"] == "+1111"
    assert body["template"]["name"] == "sos_alert"
    assert body["template"]["language"]["code"] == "en"
    params = body["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "Raj"
    assert params[1]["text"] == "http://x/m1"


def test_send_one_falls_back_to_sent_when_response_has_no_message_id(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="sos_alert")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = b"not json"
    with patch("gss.alerts.urllib.request.urlopen", return_value=fake_resp):
        ok, detail = sender._send_one(
            {"phone": "+1111"}, {"person_name": "Raj", "status_page_url": "http://x/m1"}
        )

    assert ok is True
    assert detail == "sent"


def test_send_one_uses_configured_template_lang(monkeypatch):
    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="sos_alert", template_lang="hi")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = b"{}"
    with patch("gss.alerts.urllib.request.urlopen", return_value=fake_resp) as urlopen:
        sender._send_one(
            {"phone": "+1111"}, {"person_name": "Raj", "status_page_url": "http://x/m1"}
        )

    body = json.loads(urlopen.call_args.args[0].data)
    assert body["template"]["language"]["code"] == "hi"


def test_send_one_returns_failure_on_http_error(monkeypatch):
    import urllib.error

    monkeypatch.setattr(config, "WABA_PHONE_NUMBER_ID", "123")
    monkeypatch.setattr(config, "WABA_ACCESS_TOKEN", "secret-token")
    sender = AlertSender(MagicMock(), template_name="sos_alert")

    exc = urllib.error.HTTPError(
        "http://x", 401, "Unauthorized", None, MagicMock(read=lambda: b"bad token")
    )
    with patch("gss.alerts.urllib.request.urlopen", side_effect=exc):
        ok, detail = sender._send_one(
            {"phone": "+1111"}, {"person_name": "Raj", "status_page_url": "http://x/m1"}
        )

    assert ok is False
    assert "401" in detail
    assert "bad token" in detail


# ── 5/9: the full pipeline, a raw commands row -> AlertSender.send ─────────
#
# Bug fix: _fire_sos_alert used to run inside _accept_flight, AFTER
# _validate_flight had already accepted the command -- so a geofence
# rejection, stale telemetry, a conflicting active mission, or a safety.py
# veto meant the alert never fired at all. That contradicted PROJECT.md
# Section 13 ("none of that may ever stop this alert"). The fix moved the
# call to _process(), immediately after the command is claimed and BEFORE
# _validate_flight runs -- so every test below drives the real entry point,
# intake._process(), not _accept_flight() directly (which no longer touches
# alerts at all). AlertSender.send() is called with the COMMAND's id (not a
# mission id -- see gss/alerts.py's send() docstring): no mission exists yet
# when the alert goes out, regardless of whether one gets created a moment
# later, so the status-page link uses the one identifier guaranteed to exist.

def _sos_cmd(command_id=None):
    now = datetime.now(timezone.utc)
    return {
        "id": command_id or _cid(), "type": "sos",
        "target_lat": 25.60, "target_lon": 85.21, "target_alt_m": 30.0,
        "issued_by": "phone", "params": {},
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }


def _claim(cmd):
    return (True, {"command": cmd, "server_now": datetime.now(timezone.utc).isoformat()})


def _allow_safety():
    safety = MagicMock()
    safety.evaluate_command.return_value = SimpleNamespace(action=SafetyAction.ALLOW, reasons=())
    return safety


_OK_SNAPSHOT = SimpleNamespace(connected=True, telemetry_age_s=1.0)


def test_9a_claimed_sos_row_reaches_alertsender_when_validation_passes(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    store.create_mission.return_value = "mission-9a"
    safety = _allow_safety()
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False, safety=safety)

    with patch.object(intake, "_start_executor") as start_executor, \
         patch("gss.alerts.AlertSender") as sender_cls:
        intake._process(cmd["id"], _Box())  # the real entry point, not _accept_flight

    safety.evaluate_command.assert_called_once()
    store.create_mission.assert_called_once()
    # fired with the command id, before the mission exists -- even on the
    # accepted path, since it fires from _process(), not _accept_flight()
    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
    start_executor.assert_called_once()


def test_9b_safety_veto_rejects_the_mission_but_alert_still_sent(monkeypatch):
    """The fix, locked in: a safety REJECT still stops the mission/flight --
    that authority is untouched -- but the WhatsApp alert has already gone
    out by the time _validate_flight even runs, so it is unaffected."""
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    safety = MagicMock()
    safety.evaluate_command.return_value = SimpleNamespace(
        action=SafetyAction.REJECT, reasons=("battery 12%",),
    )
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False, safety=safety)

    with patch("gss.alerts.AlertSender") as sender_cls:
        intake._process(cmd["id"], _Box())

    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
    store.create_mission.assert_not_called()
    store.update_command_status.assert_called_once()
    assert store.update_command_status.call_args.args[1] == "rejected"


def test_9c_alert_still_sent_when_geofence_rejects(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()
    cmd["target_lat"], cmd["target_lon"] = 30.0, 90.0  # far outside MAX_RADIUS_M

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False)

    with patch("gss.alerts.AlertSender") as sender_cls:
        intake._process(cmd["id"], _Box())

    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
    store.create_mission.assert_not_called()
    assert store.update_command_status.call_args.args[1] == "rejected"


def test_9d_alert_still_sent_when_telemetry_is_stale(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()
    stale_snapshot = SimpleNamespace(connected=True, telemetry_age_s=9999.0)

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    intake = CommandIntake(store, lambda: stale_snapshot, realtime_enabled=False)

    with patch("gss.alerts.AlertSender") as sender_cls:
        intake._process(cmd["id"], _Box())

    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
    store.create_mission.assert_not_called()
    assert store.update_command_status.call_args.args[1] == "rejected"


def test_5a_alert_send_raises_validation_and_mission_still_proceed(monkeypatch):
    """The alert path is caught inside _fire_sos_alert itself -- this proves
    that even so, _process() carries on through validation and acceptance
    rather than dying partway (independence holds in both directions)."""
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    store.create_mission.return_value = "mission-5a"
    safety = _allow_safety()
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False, safety=safety)

    with patch.object(intake, "_start_executor") as start_executor, \
         patch("gss.alerts.AlertSender") as sender_cls:
        sender_cls.return_value.send.side_effect = RuntimeError("whatsapp down")
        intake._process(cmd["id"], _Box())  # must not raise

    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
    store.create_mission.assert_called_once()
    store.link_command_to_mission.assert_called_once_with(cmd["id"], "mission-5a")
    start_executor.assert_called_once()


def test_5b_mission_creation_fails_alert_was_already_sent(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    cmd = _sos_cmd()

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    store.create_mission.return_value = None  # simulated failure
    safety = _allow_safety()
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False, safety=safety)

    with patch("gss.alerts.AlertSender") as sender_cls:
        intake._process(cmd["id"], _Box())  # must not raise

    # fired once, at claim time -- a later mission-creation failure does not
    # trigger (or need) a second attempt
    sender_cls.return_value.send.assert_called_once_with(cmd["id"])
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
    cmd = _sos_cmd()

    store = MagicMock()
    store.claim_command.return_value = _claim(cmd)
    store.create_mission.return_value = "mission-8"
    safety = _allow_safety()
    intake = CommandIntake(store, lambda: _OK_SNAPSHOT, realtime_enabled=False, safety=safety)

    with patch.object(intake, "_start_executor") as start_executor:
        intake._process(cmd["id"], _Box())

    store.create_alert.assert_not_called()
    store.create_mission.assert_called_once()
    start_executor.assert_called_once()


# ── 10: alerts.mission_id must stay nullable in the schema ─────────────────
#
# Bug-hunt regression guard. AlertSender._send() (gss/alerts.py) now always
# writes its `alerts` row before a mission may even exist -- mission_id=None
# is a legitimate, PERMANENT state, not a transient one, for every "sos"
# command from now on. Confirmed directly against the live Supabase project
# during the investigation into a reported "internal error while handling
# the command" crash (a real INSERT with mission_id=null succeeded; the
# crash traced to a since-stopped stale process running pre-fix code, not to
# a schema constraint -- see PROJECT.md/commit history for the writeup).
# This test cannot exercise the live database (these tests run with
# SUPABASE_ENABLED=false, no real Supabase credentials required) -- so
# instead it guards the migration FILES: a future migration that adds
# `NOT NULL` back onto `alerts.mission_id` would only ever surface as a
# production write failure, silently, since no local test touches the real
# schema. This makes that mistake fail here first.

import pathlib
import re

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "supabase" / "migrations"


def _all_migrations_sql() -> str:
    paths = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    assert paths, f"no migration files found under {_MIGRATIONS_DIR}"
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def test_10_no_migration_makes_alerts_mission_id_not_null():
    """Line-by-line, not whole-statement: a CREATE TABLE (or ALTER TABLE)
    statement mentioning "alerts" also legitimately contains "not null" for
    OTHER columns (e.g. triggered_at) -- only a line that names mission_id
    specifically, alongside "not null", is the thing to catch. Covers both
    the original create-table definition and any later ALTER TABLE."""
    sql = _all_migrations_sql()
    for stmt in re.split(r";", sql):
        if "alerts" not in stmt.lower() or "mission_id" not in stmt.lower():
            continue
        for line in stmt.splitlines():
            if re.search(r"\bmission_id\b", line, re.IGNORECASE):
                assert not re.search(r"not\s+null", line, re.IGNORECASE), (
                    f"a migration line makes alerts.mission_id NOT NULL: "
                    f"{line.strip()!r}"
                )
