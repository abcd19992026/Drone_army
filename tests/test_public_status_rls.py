"""tests/test_public_status_rls.py -- Phase 11 follow-up: public live-status-
page backend, verified against the REAL Supabase project in .env.

Unlike tests/test_alerts.py (MagicMock store, no network), this test suite
needs the real project because the thing under test IS the database's access
control -- Postgres RLS and a security-definer function cannot be
meaningfully mocked. It uses the anon/publishable key from web/config.js
(gitignored, browser-facing -- the SAME key the actual public status page
will use), never the service_role key from .env, which bypasses RLS entirely
and would make every assertion here pass regardless of whether the database
is actually configured correctly.

Skips cleanly (not a failure) when:
  * no anon key is available (web/config.js absent -- e.g. a fresh checkout
    that hasn't run the web app setup), or
  * supabase/migrations/20260918120000_public_sos_status.sql has not been
    applied to this project yet (the RPC genuinely does not exist) -- this
    repo's migrations are written here and applied by the operator
    separately (`supabase db push` or the SQL editor), the same as every
    other migration in supabase/migrations/.

    python -m pytest tests/test_public_status_rls.py -v
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from gss import config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_HINT = (
    "supabase/migrations/20260918120000_public_sos_status.sql has not been "
    "applied to this project yet (run `supabase db push` after `supabase "
    "link`, or paste the file into the SQL editor) -- skipping"
)


def _load_anon_key() -> str | None:
    """web/config.js is the only place in this repo the anon key lives."""
    path = _REPO_ROOT / "web" / "config.js"
    if not path.exists():
        return None
    match = re.search(r'SUPABASE_ANON_KEY:\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


_ANON_KEY = _load_anon_key()
_BASE = config.SUPABASE_URL.rstrip("/") if config.SUPABASE_URL else None

pytestmark = pytest.mark.skipif(
    not (_ANON_KEY and _BASE),
    reason="no anon key (web/config.js) / SUPABASE_URL configured -- these "
    "tests need the real live Supabase project, not a mock",
)


def _get(path: str, key: str) -> tuple[int, object]:
    request = urllib.request.Request(
        _BASE + path, headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _anon_get(path: str) -> tuple[int, object]:
    return _get(path, _ANON_KEY)


def _service_get(path: str) -> tuple[int, object]:
    return _get(path, config.SUPABASE_SERVICE_ROLE_KEY)


def _rpc(command_id: str) -> tuple[int, object]:
    return _anon_get(f"/rest/v1/rpc/get_sos_incident_status?p_command_id={command_id}")


# ── anon must not read the raw tables at all, narrow RPC or not ────────────
#
# PostgREST's answer to "RLS enabled, no policy for this role" is 200 + an
# empty array, not 401/403 -- so the only meaningful assertion is "the
# service-role key (which bypasses RLS) proves rows exist, but anon sees
# none of them", not "anon gets an error status".

def test_anon_cannot_read_contacts_table():
    _, service_rows = _service_get("/rest/v1/contacts?select=id&limit=1")
    if not service_rows:
        pytest.skip("no contacts row exists to prove anon can't see it -- "
                    "add one with scripts/manage_contacts.py first")
    status, rows = _anon_get("/rest/v1/contacts?select=*")
    assert status == 200
    assert rows == [], "anon can read the contacts table directly -- RLS regression"


def test_anon_cannot_read_commands_table():
    _, service_rows = _service_get("/rest/v1/commands?select=id&limit=1")
    if not service_rows:
        pytest.skip("no commands row exists to prove anon can't see it")
    status, rows = _anon_get("/rest/v1/commands?select=*")
    assert status == 200
    assert rows == [], "anon can read the commands table directly -- RLS regression"


def test_anon_cannot_read_missions_or_locations_or_alerts_tables():
    for table in ("missions", "locations", "alerts"):
        _, service_rows = _service_get(f"/rest/v1/{table}?select=id&limit=1")
        if not service_rows:
            continue  # nothing there to prove denial against -- not a failure
        status, rows = _anon_get(f"/rest/v1/{table}?select=*")
        assert status == 200
        assert rows == [], f"anon can read {table} directly -- RLS regression"


# ── the narrow RPC itself ───────────────────────────────────────────────────

def test_anon_cannot_enumerate_all_incidents():
    """Calling with no p_command_id must never succeed -- whether because
    the function genuinely doesn't exist yet (migration not applied) or
    because PostgREST refuses a call missing a required parameter, both are
    "denied", which is the property being verified: there is no way to list
    every live incident through this surface."""
    status, body = _anon_get("/rest/v1/rpc/get_sos_incident_status")
    assert status != 200, f"calling the RPC with no id should be refused, got {body!r}"


def test_anon_can_call_the_narrow_rpc_for_an_unknown_id_and_gets_nothing():
    status, body = _rpc(str(uuid.uuid4()))
    if status == 404:
        pytest.skip(_MIGRATION_HINT)
    assert status == 200
    assert body == [], "an unknown command id must return no rows, not an error"


def test_anon_reading_a_real_sos_command_gets_only_the_narrow_fields():
    """The positive path: a real 'sos' command's id, queried with the anon
    key, returns exactly the pre-selected columns -- never target_lat,
    issued_by, params, or anything else `commands`/`missions` actually
    hold."""
    _, service_rows = _service_get(
        "/rest/v1/commands?type=eq.sos&select=id&order=created_at.desc&limit=1"
    )
    if not service_rows:
        pytest.skip("no real 'sos' command exists in this project to check against")
    command_id = service_rows[0]["id"]

    status, body = _rpc(command_id)
    if status == 404:
        pytest.skip(_MIGRATION_HINT)
    assert status == 200
    assert len(body) == 1
    row = body[0]
    assert set(row) == {
        "command_id", "command_status", "command_created_at",
        "plain_status", "reason", "mission_id", "recent_locations",
    }
    assert row["command_id"] == command_id
    assert row["plain_status"] in (
        "not_dispatched", "dispatched", "enroute", "returned", "expired",
    )


def test_anon_reading_a_non_sos_command_gets_nothing():
    """Scoped to sos-type incidents only -- a real 'goto'/'summon'/etc.
    command's id must not resolve through this surface even though the row
    itself exists."""
    _, service_rows = _service_get(
        "/rest/v1/commands?type=neq.sos&select=id&order=created_at.desc&limit=1"
    )
    if not service_rows:
        pytest.skip("no non-sos command exists in this project to check against")
    command_id = service_rows[0]["id"]

    status, body = _rpc(command_id)
    if status == 404:
        pytest.skip(_MIGRATION_HINT)
    assert status == 200
    assert body == [], "a non-sos command must not be exposed by the public status RPC"
