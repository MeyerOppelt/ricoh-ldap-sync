"""Work out and apply the changes that bring a printer's address book in line with LDAP.

Ownership lives in the state file: printer name -> email -> record, where a
record is ``{"id": <entry id or null>, "name": <name written>, "synced_at": <ISO time>}``.
The sync only updates or deletes entries listed there. A record with ``"id": null``
is an add that was sent but not yet confirmed. The next run adopts the entry if it
landed, or adds it again if it didn't.

With KEEP_SORTED, the sync keeps its own entries in alphabetical order by
registration number. When the order is wrong it deletes all of its entries and
re-adds them in name order, so the printer numbers them from the lowest free
registration number upwards. Before deleting, every record is turned into an
unconfirmed add (``"id": null``), so if the run is interrupted the next run
re-adds whoever is missing. Records may also carry ``"rebuild"`` / ``"retire"``
markers left by 0.2.0, which renumbered by copying first; the plan still
cleans those up.
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


def sort_key(name: str, email: str = "") -> tuple[str, str]:
    """Alphabetical order ignoring accents and case; email breaks ties."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return (plain.casefold(), email)


def registration_number(entry: Entry) -> int:
    value = entry.props.get("index", "")
    return int(value) if value.isdigit() else entry.id


def in_order(positions_and_keys: list[tuple[int, tuple[str, str]]]) -> bool:
    keys = [key for _, key in sorted(positions_and_keys)]
    return keys == sorted(keys)


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
    retire: list[tuple[int, str]] = field(default_factory=list)  # leftover copies from an interrupted renumber
    renumber: int = 0  # entries a renumber would re-add (estimate; the live run checks again)

    @property
    def changes(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.deletes) + len(self.adoptions)


def plan(printer: str, users: list[User], entries: list[Entry], records: Records | None,
         max_delete_fraction: float, capacity: int | None = None, count: int | None = None,
         keep_sorted: bool = False) -> Plan:
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

    retire: dict[int, str] = {}
    for email, rec in valid.items():
        for old in rec.get("retire", []):
            entry = by_id.get(old)
            if entry is not None and entry.email == email and old != rec["id"]:
                retire[old] = email
        if rec.get("rebuild"):
            for entry in by_email.get(email, []):
                if entry.id != rec["id"] and entry.name == rec.get("name"):
                    retire[entry.id] = email
    p.retire = sorted(retire.items())
    owned.update(retire)

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
    p.adds.sort(key=lambda d: sort_key(d.name, d.email))

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

    if keep_sorted:
        deleted = {email for _, email, _ in p.deletes}
        final = [(registration_number(by_id[rec["id"]]), sort_key(desired(wanted[email]).name, email))
                 for email, rec in valid.items() if email in wanted and email not in deleted]
        final += [(registration_number(by_id[i]), sort_key(d.name, d.email)) for i, d in p.adoptions]
        # New entries usually get numbers after the existing ones.
        final += [(10**9 + n, sort_key(d.name, d.email)) for n, d in enumerate(p.adds)]
        if not in_order(final):
            p.renumber = len(final)
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
    for entry_id, email in p.retire:
        action(p.printer, "retire", email, result, entry=entry_id, reason="copy left by an interrupted renumber")
    if p.renumber:
        action(p.printer, "renumber", "*", result if dry_run else "will check after changes", entries=p.renumber)
    if p.brake_reason:
        log.error("printer=%s safety brake: %s; skipping all %d deletes", p.printer, p.brake_reason, len(p.braked))
        for entry_id, email, name in p.braked:
            action(p.printer, "delete", email, "blocked by safety brake", logging.ERROR, entry=entry_id, name=name)
    log.info("printer=%s plan: %d add, %d update, %d delete, %d adopt, %d unchanged, %d manual%s",
             p.printer, len(p.adds), len(p.updates), len(p.deletes), len(p.adoptions), len(p.unchanged),
             len(p.manual), " (dry run, nothing written)" if dry_run else "")


def sync_printer(client: RicohClient, printer: str, users: list[User], records: Records | None, *,
                 dry_run: bool, max_delete_fraction: float, capacity: int | None, save,
                 keep_sorted: bool = False, force_renumber: bool = False) -> bool:
    """Sync one printer. ``save(records)`` persists this printer's records after every change.

    Returns True when every planned change succeeded. Raises RicohError when the
    printer can't be read at all.
    """
    with client.session(write=not dry_run):
        ids, count = client.search_ids()
        entries = client.get_entries(ids)
        log.info("printer=%s address book: %d entries", printer, count)
        p = plan(printer, users, entries, records, max_delete_fraction, capacity, count, keep_sorted)
        log_plan(p, dry_run)
        if dry_run:
            return True
        records = dict(records or {})
        ok = _apply(client, p, records, set(ids), save)
        if keep_sorted or force_renumber:
            ok &= _renumber(client, printer, records, {u.email: u for u in users}, save, force_renumber)
        return ok


def _apply(client: RicohClient, p: Plan, records: Records, before_ids: set[int], save) -> bool:
    ok = True
    printer = p.printer
    if p.retire:
        try:
            client.delete_entries([entry_id for entry_id, _ in p.retire])
            for entry_id, email in p.retire:
                action(printer, "retire", email, "ok", entry=entry_id)
        except RicohError as e:
            ok = False
            log.error("printer=%s could not delete copies left by an interrupted renumber: %s", printer, e)
    if ok:
        for rec in records.values():
            rec.pop("retire", None)
            rec.pop("rebuild", None)
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


def _renumber(client: RicohClient, printer: str, records: Records, users: dict[str, User], save,
              force: bool = False) -> bool:
    """Delete this printer's owned entries and re-add them in name order if they're out of order (or if forced)."""
    if any(rec.get("retire") or rec.get("rebuild") for rec in records.values()):
        log.error("printer=%s renumber skipped: copies from an earlier renumber still need deleting", printer)
        return False
    if any(rec.get("id") is None for email, rec in records.items() if email in users):
        log.error("printer=%s renumber skipped: some adds are unconfirmed; the next run settles them first", printer)
        return False
    ids, _ = client.search_ids()
    entries = {e.id: e for e in client.get_entries(ids)}
    owned = [(email, rec, entries[rec["id"]]) for email, rec in records.items()
             if email in users and rec.get("id") in entries and entries[rec["id"]].email == email]
    if any(rec["name"] != desired(users[email]).name for email, rec, _ in owned):
        log.error("printer=%s renumber skipped: an earlier change in this run failed", printer)
        return False
    if not owned or (not force and in_order([(registration_number(e), sort_key(rec["name"], email))
                                             for email, rec, e in owned])):
        return True

    ordered = sorted(owned, key=lambda o: sort_key(o[1]["name"], o[0]))
    log.info("printer=%s renumber: deleting %d entries and re-adding them in name order", printer, len(ordered))
    # From here until each re-add is confirmed, these people are off the panel.
    # Recording them as unconfirmed adds first means an interrupted run is
    # finished by the next one, which re-adds whoever is missing.
    for _, rec, _ in ordered:
        rec["id"] = None
    save(records)

    try:
        for start in range(0, len(ordered), 50):
            client.delete_entries([old.id for _, _, old in ordered[start:start + 50]])
    except RicohError as e:
        log.error("printer=%s renumber stopped: deleting failed (%s); nothing was re-added", printer, e)
        try:
            still_there = set(client.search_ids()[0])
        except RicohError:
            still_there = set()
        for _, rec, old in ordered:
            if old.id in still_there:
                rec["id"] = old.id
        save(records)
        return False

    before = set(client.search_ids()[0])
    added: list[str] = []
    error = None
    for email, _, _ in ordered:
        try:
            client.add_entry(desired(users[email]).add_props())
        except RicohError as e:
            error = f"re-adding {email} failed: {e}"
            break
        added.append(email)

    now = _now()
    new_ids, _ = client.search_ids()
    new = client.get_entries([i for i in new_ids if i not in before])
    for email in added:
        rec = records[email]
        match = next((e for e in new if e.email == email and e.name == rec["name"]), None)
        if match is None:
            error = error or f"the re-added entry for {email} was not found"
            continue
        rec["id"] = match.id
        rec["synced_at"] = now
    save(records)
    if error:
        missing = sum(1 for _, rec, _ in ordered if rec["id"] is None)
        log.error("printer=%s renumber incomplete: %s; %d entries will be re-added on the next run",
                  printer, error, missing)
        return False
    action(printer, "renumber", "*", "ok", entries=len(ordered))
    return True
