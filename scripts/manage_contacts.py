"""scripts/manage_contacts.py -- add/list/remove SOS trusted contacts (Phase 11).

The ``contacts`` table (supabase/migrations/20260910063937_initial_schema.sql)
is what gss/alerts.py's ``AlertSender`` reads, via
:meth:`gss.store.TelemetryStore.get_active_contacts`, before firing a WhatsApp
SOS alert -- with nobody in it (``active = true``), every "sos" command's
alert fails with "no active contacts configured". This script is the
supported way to populate/manage it: no manual Supabase-dashboard editing, no
migration, no change to the schema or to gss/safety.py or gss/mission.py.

Real columns (confirmed against the migration, not PROJECT.md's draft):
    id          uuid, generated
    name        text, required
    phone       text, E.164 (e.g. +91XXXXXXXXXX) -- alerts.py silently drops
                a contact with no phone (nothing to send them), so this
                script requires one
    relation    text, free-form (e.g. "wife", "father", "friend")
    priority    integer, default 100 -- HIGHER fires first
                (gss.store.get_active_contacts orders priority.desc)
    active      boolean, default true -- only active=true rows are ever
                notified; "remove" below sets this false rather than
                deleting the row, so the alert history stays intact

Uses the same Supabase REST + service-role-key setup as the rest of gss/
(config.SUPABASE_URL / config.SUPABASE_SERVICE_ROLE_KEY, urllib.request --
no new dependency, no client library).

Usage:
    python scripts/manage_contacts.py add                     # interactive
    python scripts/manage_contacts.py add "Name" "+91XXXXXXXXXX" "relation" [priority]
    python scripts/manage_contacts.py list
    python scripts/manage_contacts.py remove <id-or-phone>     # sets active=false
    python scripts/manage_contacts.py activate <id-or-phone>   # sets active=true
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gss import config  # noqa: E402

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None,
             prefer: str = "return=representation") -> tuple[int, object]:
    """POST/GET/PATCH against the Supabase REST API. Returns (status, parsed_json)."""
    base = config.SUPABASE_URL.rstrip("/")
    if not base or not config.SUPABASE_SERVICE_ROLE_KEY:
        print(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured "
            "(check .env) -- cannot reach the contacts table.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"Supabase request failed: HTTP {exc.code}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Supabase request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _validate_phone(phone: str) -> str | None:
    """Return an error message, or None if ``phone`` is valid E.164."""
    if not _E164_RE.match(phone.strip()):
        return (
            f"{phone!r} does not look like E.164 (e.g. +91XXXXXXXXXX -- a "
            f"'+' then 8-15 digits, no spaces/dashes)"
        )
    return None


def _prompt_contact() -> tuple[str, str, str, int]:
    """Interactively prompt for a contact's fields. Retries the phone number
    until it passes the E.164 check rather than saving something alerts.py
    will just silently drop later."""
    name = input("Name: ").strip()
    while not name:
        name = input("Name (required): ").strip()

    while True:
        phone = input("Phone (E.164, e.g. +91XXXXXXXXXX): ").strip()
        error = _validate_phone(phone)
        if error is None:
            break
        print(f"  {error}")

    relation = input("Relation (e.g. wife, father, friend) [optional]: ").strip() or None

    while True:
        raw_priority = input("Priority [default 100, higher fires first]: ").strip()
        if not raw_priority:
            priority = 100
            break
        try:
            priority = int(raw_priority)
            break
        except ValueError:
            print("  priority must be a whole number")

    return name, phone, relation, priority


def cmd_add(args: list[str]) -> None:
    if not args:
        name, phone, relation, priority = _prompt_contact()
    elif len(args) in (3, 4):
        name, phone, relation = args[0], args[1], args[2]
        priority = int(args[3]) if len(args) == 4 else 100
        error = _validate_phone(phone)
        if error:
            print(f"Refusing to add: {error}", file=sys.stderr)
            raise SystemExit(1)
    else:
        print(
            "Usage: manage_contacts.py add [\"Name\" \"+91XXXXXXXXXX\" \"relation\" [priority]]\n"
            "       (run with no arguments to be prompted interactively)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    status, rows = _request(
        "POST", "/rest/v1/contacts",
        body={
            "name": name, "phone": phone, "relation": relation,
            "priority": priority, "active": True,
        },
    )
    row = rows[0]
    print(f"Added contact {row['id']} -- {row['name']} ({row['phone']}), "
          f"relation={row['relation']!r}, priority={row['priority']}, active={row['active']}")


def cmd_list(_args: list[str]) -> None:
    status, rows = _request(
        "GET", "/rest/v1/contacts",
        params={"select": "id,name,phone,relation,priority,active,created_at",
                "order": "priority.desc"},
    )
    if not rows:
        print("No contacts configured -- every SOS alert will fail with "
              "'no active contacts configured' until you add one.")
        return
    for row in rows:
        flag = "ACTIVE  " if row["active"] else "inactive"
        print(f"[{flag}] {row['id']}  {row['name']:<20} {row['phone'] or '(no phone)':<16} "
              f"relation={row['relation'] or '-':<10} priority={row['priority']}")


def _find_filter(identifier: str) -> dict:
    if _UUID_RE.match(identifier):
        return {"id": f"eq.{identifier}"}
    return {"phone": f"eq.{identifier}"}


def _set_active(identifier: str, active: bool) -> None:
    status, rows = _request(
        "PATCH", "/rest/v1/contacts", params=_find_filter(identifier), body={"active": active},
    )
    if not rows:
        print(f"No contact found matching {identifier!r} (by id or phone)", file=sys.stderr)
        raise SystemExit(1)
    for row in rows:
        print(f"{'Activated' if active else 'Deactivated'} {row['id']} -- "
              f"{row['name']} ({row['phone']}); active={row['active']}")


def cmd_remove(args: list[str]) -> None:
    if len(args) != 1:
        print("Usage: manage_contacts.py remove <id-or-phone>", file=sys.stderr)
        raise SystemExit(2)
    _set_active(args[0], active=False)


def cmd_activate(args: list[str]) -> None:
    if len(args) != 1:
        print("Usage: manage_contacts.py activate <id-or-phone>", file=sys.stderr)
        raise SystemExit(2)
    _set_active(args[0], active=True)


_COMMANDS = {"add": cmd_add, "list": cmd_list, "remove": cmd_remove, "activate": cmd_activate}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in _COMMANDS:
        print(__doc__)
        raise SystemExit(2 if argv else 0)
    _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
