"""Work out and apply the changes that bring a printer's address book in line with LDAP.

Ownership lives in the state file: printer name -> email -> record, where a
record is ``{"id": <entry id or null>, "name": <name written>, "synced_at": <ISO time>}``.
The sync only updates or deletes entries listed there. A record with ``"id": null``
is an add that was sent but not yet confirmed. The next run adopts the entry if it
landed, or adds it again if it didn't.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ldap_source import User
from .ricoh_soap import Entry, RicohClient, RicohError

log = logging.getLogger(__name__)

# Field limits from the address book's input rules.
NAME_MAX = 20
KEY_DISPLAY_MAX = 16
DISPLAY_PRIORITY = "5"  # the printer's default priority

# Title1 index tabs. Tag 1 is "Frequent"; tags 2-11 are these groups in order.
TITLE1_GROUPS = ("AB", "CD", "EF", "GH", "IJK", "LMN", "OPQ", "RST", "UVW", "XYZ")

Records = dict[str, dict]


class StateError(Exception):
    pass


# -- what to write ---------------------------------------------------------

def title1_tag(name: str) -> int | None:
    """Title1 tag ID for the first letter of name (accents ignored), or None if it has no A-Z letter."""
    for ch in unicodedata.normalize("NFKD", name).upper():
        if "A" <= ch <= "Z":
            return next(i + 2 for i, group in enumerate(TITLE1_GROUPS) if ch in group)
    return None


def truncate(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit].rstrip()


@dataclass(frozen=True)
class Desired:
    email: str
    name: str
    key_display: str
    tag: int | None

    def tag_value(self) -> str:
        # An entry needs at least one tag to show on the panel; fall back to Frequent.
        return str(self.tag if self.tag is not None else 1)

    def add_props(self) -> list[tuple[str, str]]:
        return [
            ("entryType", "user"),
            ("name", self.name),
            ("longName", self.key_display),
            ("displayedOrder", DISPLAY_PRIORITY),
            ("tagId", self.tag_value()),
            ("mail:", "true"),
            ("mail:address", self.email),
            ("isSender", "false"),
            ("isDestination", "true"),
        ]

    def update_props(self) -> list[tuple[str, str]]:
        return [("name", self.name), ("longName", self.key_display), ("tagId", self.tag_value())]


def desired(user: User) -> Desired:
    return Desired(
        email=user.email,
        name=truncate(user.name, NAME_MAX),
        key_display=truncate(user.name, KEY_DISPLAY_MAX),
        tag=title1_tag(user.name),
    )


# -- the plan --------------------------------------------------------------

@dataclass
class Plan:
    printer: str
    adds: list[Desired] = field(default_factory=list)
    updates: list[tuple[int, Desired, str]] = field(default_factory=list)  # id, new, old name
    deletes: list[tuple[int, str, str]] = field(default_factory=list)  # id, email, name
    adoptions: list[tuple[int, Desired]] = field(default_factory=list)
    forgotten: list[tuple[str, str]] = field(default_factory=list)  # email, reason
    manual: list[tuple[str, int]] = field(default_factory=list)  # email, id of the hand-made entry
    full: list[Desired] = field(default_factory=list)
    braked: list[tuple[int, str, str]] = field(default_factory=list)
    brake_reason: str | None = None
    unchanged: list[str] = field(default_factory=list)  # emails already correct

    @property
    def changes(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.deletes) + len(self.adoptions)


def plan(printer: str, users: list[User], entries: list[Entry], records: Records | None,
         max_delete_fraction: float, capacity: int | None = None, count: int | None = None) -> Plan:
    """Compare LDAP users with the printer's entries. ``records`` is None when no state exists for this printer."""
    p = Plan(printer)
    state_known = records is not None
    records = records or {}
    by_id = {e.id: e for e in entries}
    by_email: dict[str, list[Entry]] = {}
    for e in entries:
        if e.email:
            by_email.setdefault(e.email, []).append(e)
    wanted = {u.email: u for u in users}

    valid: Records = {}
    pending: Records = {}
    owned: set[int] = set()
    for email, rec in records.items():
        entry_id = rec.get("id")
        if entry_id is None:
            pending[email] = rec
            continue
        entry = by_id.get(entry_id)
        if entry is None:
            p.forgotten.append((email, f"entry {entry_id} is no longer on the printer"))
        elif entry.email != email:
            p.forgotten.append((email, f"entry {entry_id} now has a different email"))
        else:
            valid[email] = rec
            owned.add(entry_id)

    for email, rec in pending.items():
        if email not in wanted:
            p.forgotten.append((email, "unconfirmed add for a user no longer in LDAP; check the printer by hand"))

    for user in users:
        d = desired(user)
        if user.email in valid:
            rec = valid[user.email]
            if rec.get("name") != d.name:
                p.updates.append((rec["id"], d, rec.get("name", "")))
            else:
                p.unchanged.append(user.email)
            continue
        others = [e for e in by_email.get(user.email, []) if e.id not in owned]
        if others:
            exact = next((e for e in others if e.name == d.name), None)
            if exact is not None and (not state_known or user.email in pending):
                p.adoptions.append((exact.id, d))
                owned.add(exact.id)
            else:
                if user.email in pending:
                    p.forgotten.append((user.email, "unconfirmed add not found; another entry has this email"))
                p.manual.append((user.email, others[0].id))
            continue
        p.adds.append(d)

    deletes = [(rec["id"], email, rec.get("name", "")) for email, rec in valid.items() if email not in wanted]
    if deletes:
        if not users:
            p.brake_reason = "LDAP returned 0 users"
        elif len(deletes) / len(valid) > max_delete_fraction:
            p.brake_reason = (f"{len(deletes)} of {len(valid)} managed entries would be deleted, "
                              f"more than MAX_DELETE_FRACTION={max_delete_fraction}")
    if p.brake_reason:
        p.braked = sorted(deletes)
    else:
        p.deletes = sorted(deletes)

    if capacity is not None and count is not None:
        free = max(capacity - count + len(p.deletes), 0)
        if len(p.adds) > free:
            p.adds, p.full = p.adds[:free], p.adds[free:]
    return p


# -- state file ------------------------------------------------------------

def load_state(path: str) -> dict[str, Records] | None:
    """The state, or None if the file doesn't exist. A corrupt file raises StateError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise StateError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise StateError(f"{path} is not a printer -> email -> record mapping")
    return data


def save_state(path: str, state: dict[str, Records]) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- applying a plan -------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def action(printer: str, act: str, email: str, result: str, level=logging.INFO, **extra) -> None:
    details = "".join(f' {k}="{v}"' if isinstance(v, str) else f" {k}={v}" for k, v in extra.items())
    log.log(level, "printer=%s action=%s email=%s%s result=%s", printer, act, email, details, result)


def log_plan(p: Plan, dry_run: bool) -> None:
    result = "planned" if dry_run else "pending"
    for email, reason in p.forgotten:
        action(p.printer, "forget", email, reason, logging.WARNING)
    for email, entry_id in p.manual:
        action(p.printer, "skip", email, "a manually created entry already has this email; leaving it alone",
               logging.WARNING, entry=entry_id)
    for entry_id, d in p.adoptions:
        action(p.printer, "adopt", d.email, result, entry=entry_id, name=d.name)
    for entry_id, email, name in p.deletes:
        action(p.printer, "delete", email, result, entry=entry_id, name=name)
    for entry_id, d, old in p.updates:
        action(p.printer, "update", d.email, result, entry=entry_id, name=d.name, was=old)
    for d in p.adds:
        action(p.printer, "add", d.email, result, name=d.name, key_display=d.key_display)
    for d in p.full:
        action(p.printer, "add", d.email, "skipped: address book is full", logging.ERROR, name=d.name)
    if p.brake_reason:
        log.error("printer=%s safety brake: %s; skipping all %d deletes", p.printer, p.brake_reason, len(p.braked))
        for entry_id, email, name in p.braked:
            action(p.printer, "delete", email, "blocked by safety brake", logging.ERROR, entry=entry_id, name=name)
    log.info("printer=%s plan: %d add, %d update, %d delete, %d adopt, %d unchanged, %d manual%s",
             p.printer, len(p.adds), len(p.updates), len(p.deletes), len(p.adoptions), len(p.unchanged),
             len(p.manual), " (dry run, nothing written)" if dry_run else "")


def sync_printer(client: RicohClient, printer: str, users: list[User], records: Records | None, *,
                 dry_run: bool, max_delete_fraction: float, capacity: int | None, save) -> bool:
    """Sync one printer. ``save(records)`` persists this printer's records after every change.

    Returns True when every planned change succeeded. Raises RicohError when the
    printer can't be read at all.
    """
    with client.session(write=not dry_run):
        ids, count = client.search_ids()
        entries = client.get_entries(ids)
        log.info("printer=%s address book: %d entries", printer, count)
        p = plan(printer, users, entries, records, max_delete_fraction, capacity, count)
        log_plan(p, dry_run)
        if dry_run:
            return True
        return _apply(client, p, dict(records or {}), set(ids), save)


def _apply(client: RicohClient, p: Plan, records: Records, before_ids: set[int], save) -> bool:
    ok = True
    printer = p.printer
    for email, _ in p.forgotten:
        records.pop(email, None)
    for entry_id, d in p.adoptions:
        records[d.email] = {"id": entry_id, "name": d.name, "synced_at": _now()}
        action(printer, "adopt", d.email, "ok", entry=entry_id, name=d.name)
    save(records)

    for entry_id, email, name in p.deletes:
        try:
            client.delete_entries([entry_id])
        except RicohError as e:
            ok = False
            action(printer, "delete", email, f"error: {e}", logging.ERROR, entry=entry_id)
            continue
        records.pop(email, None)
        save(records)
        action(printer, "delete", email, "ok", entry=entry_id, name=name)

    for entry_id, d, old in p.updates:
        try:
            client.update_entry(entry_id, d.update_props())
        except RicohError as e:
            ok = False
            action(printer, "update", d.email, f"error: {e}", logging.ERROR, entry=entry_id)
            continue
        records[d.email] = {"id": entry_id, "name": d.name, "synced_at": _now()}
        save(records)
        action(printer, "update", d.email, "ok", entry=entry_id, name=d.name, was=old)

    sent: list[Desired] = []
    for d in p.adds:
        # Record the add before sending it, so a crash can't leave an entry we don't know we made.
        records[d.email] = {"id": None, "name": d.name, "synced_at": None}
        save(records)
        try:
            client.add_entry(d.add_props())
        except RicohError as e:
            ok = False
            records.pop(d.email, None)
            save(records)
            action(printer, "add", d.email, f"error: {e}", logging.ERROR, name=d.name)
            log.error("printer=%s stopping adds for this run after an error", printer)
            break
        sent.append(d)

    if sent:
        ids, _ = client.search_ids()
        new = [e for e in client.get_entries([i for i in ids if i not in before_ids])]
        for d in sent:
            match = next((e for e in new if e.email == d.email and e.name == d.name), None)
            if match is None:
                ok = False
                action(printer, "add", d.email, "sent, but the new entry was not found; the next run will check again",
                       logging.ERROR, name=d.name)
                continue
            records[d.email] = {"id": match.id, "name": d.name, "synced_at": _now()}
            action(printer, "add", d.email, "ok", entry=match.id, name=d.name)
        save(records)

    now = _now()
    for email in p.unchanged:
        if email in records:
            records[email]["synced_at"] = now
    save(records)
    return ok
