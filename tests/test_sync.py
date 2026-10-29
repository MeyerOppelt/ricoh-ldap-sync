import json
import logging

import pytest

from fakeprinter import FakePrinter, fault, person
from ricoh_ldap_sync import sync
from ricoh_ldap_sync.ldap_source import User
from ricoh_ldap_sync.ricoh_soap import Entry, RicohClient

ALICE = User("alice@example.com", "Alice Anders")
BOB = User("bob@example.com", "Bob Brown")
CAROL = User("carol@example.com", "Carol Clark")


def entry(entry_id, name, email):
    return Entry(entry_id, {"id": str(entry_id), "name": name, "mail:": "true", "mail:address": email})


def rec(entry_id, name):
    return {"id": entry_id, "name": name, "synced_at": "2026-01-01T00:00:00+00:00"}


# -- names and tags ----------------------------------------------------------

@pytest.mark.parametrize("name, tag", [
    ("Alice", 2), ("bob", 2), ("Carol", 3), ("Dan", 3), ("Eve", 4), ("Frank", 4), ("Gina", 5), ("Hal", 5),
    ("Ivy", 6), ("Jo", 6), ("Kim", 6), ("Lee", 7), ("Max", 7), ("Ned", 7), ("Olga", 8), ("Pat", 8), ("Quinn", 8),
    ("Rob", 9), ("Sam", 9), ("Tom", 9), ("Uma", 10), ("Vic", 10), ("Wes", 10), ("Xia", 11), ("Yan", 11), ("Zoe", 11),
    ("Élodie", 4), ("  zed", 11), ("3M Service", 7), ("123", None), ("", None),
])
def test_title1_tag(name, tag):
    assert sync.title1_tag(name) == tag


def test_truncation():
    d = sync.desired(User("x@example.com", "Christopher Montgomery-Smithson"))
    assert d.name == "Christopher Montgome" and len(d.name) == sync.NAME_MAX
    assert d.key_display == "Christopher Mont" and len(d.key_display) == sync.KEY_DISPLAY_MAX
    assert sync.truncate("Ann   Lee  ", 20) == "Ann Lee"
    assert sync.truncate("Abcdefghijklmnop qrs", 17) == "Abcdefghijklmnop"


def test_add_props():
    props = dict(sync.desired(ALICE).add_props())
    assert props == {"entryType": "user", "name": "Alice Anders", "longName": "Alice Anders", "displayedOrder": "5",
                     "tagId": "2", "mail:": "true", "mail:address": "alice@example.com", "isSender": "false",
                     "isDestination": "true"}
    assert dict(sync.desired(User("n@example.com", "123")).add_props())["tagId"] == "1"


# -- planning ----------------------------------------------------------------

def test_add_update_delete():
    entries = [entry(1, "Alice", "alice@example.com"), entry(2, "Bob Brown", "bob@example.com"),
               entry(3, "Dave", "dave@example.com")]
    records = {"alice@example.com": rec(1, "Alice"), "bob@example.com": rec(2, "Bob Brown"),
               "dave@example.com": rec(3, "Dave")}
    p = sync.plan("office", [ALICE, BOB, CAROL], entries, records, max_delete_fraction=0.5)
    assert [d.email for d in p.adds] == ["carol@example.com"]
    assert [(i, d.name, old) for i, d, old in p.updates] == [(1, "Alice Anders", "Alice")]
    assert p.deletes == [(3, "dave@example.com", "Dave")]
    assert p.unchanged == ["bob@example.com"]
    assert p.brake_reason is None


def test_safety_brake_fraction():
    entries = [entry(i, f"U{i}", f"u{i}@example.com") for i in range(1, 11)]
    records = {f"u{i}@example.com": rec(i, f"U{i}") for i in range(1, 11)}
    keep = [User(f"u{i}@example.com", f"U{i}") for i in range(1, 7)]  # 4 of 10 would go
    p = sync.plan("office", keep, entries, records, max_delete_fraction=0.3)
    assert p.deletes == [] and len(p.braked) == 4 and "MAX_DELETE_FRACTION" in p.brake_reason
    keep = [User(f"u{i}@example.com", f"U{i}") for i in range(1, 8)]  # 3 of 10: allowed
    p = sync.plan("office", keep, entries, records, max_delete_fraction=0.3)
    assert len(p.deletes) == 3 and p.brake_reason is None


def test_safety_brake_zero_users():
    records = {"alice@example.com": rec(1, "Alice Anders")}
    p = sync.plan("office", [], [entry(1, "Alice Anders", "alice@example.com")], records, max_delete_fraction=1.0)
    assert p.deletes == [] and p.braked and p.brake_reason == "LDAP returned 0 users"


def test_never_touches_unowned_entries():
    entries = [entry(1, "Manual Person", "manual@example.com"), entry(2, "Old Alice", "alice@example.com")]
    p = sync.plan("office", [ALICE], entries, {}, max_delete_fraction=1.0)
    assert p.deletes == [] and p.updates == [] and p.adds == []
    assert p.manual == [("alice@example.com", 2)]


def test_manual_duplicate_not_adopted_even_if_exact_when_state_exists():
    entries = [entry(5, "Alice Anders", "alice@example.com")]
    p = sync.plan("office", [ALICE], entries, {}, max_delete_fraction=1.0)
    assert p.manual == [("alice@example.com", 5)] and p.adoptions == [] and p.adds == []


def test_missing_state_adopts_exact_matches_only_and_deletes_nothing():
    entries = [entry(5, "Alice Anders", "alice@example.com"), entry(6, "Bobby", "bob@example.com"),
               entry(7, "Gone User", "gone@example.com")]
    p = sync.plan("office", [ALICE, BOB, CAROL], entries, None, max_delete_fraction=1.0)
    assert [(i, d.email) for i, d in p.adoptions] == [(5, "alice@example.com")]
    assert p.manual == [("bob@example.com", 6)]
    assert [d.email for d in p.adds] == ["carol@example.com"]
    assert p.deletes == [] and p.braked == []


def test_entry_removed_by_hand_is_forgotten_and_re_added():
    records = {"alice@example.com": rec(9, "Alice Anders")}
    p = sync.plan("office", [ALICE], [], records, max_delete_fraction=1.0)
    assert p.forgotten and p.forgotten[0][0] == "alice@example.com"
    assert [d.email for d in p.adds] == ["alice@example.com"]


def test_reused_id_with_other_email_is_not_ours():
    records = {"alice@example.com": rec(1, "Alice Anders")}
    p = sync.plan("office", [], [entry(1, "Someone", "someone@example.com")], records, max_delete_fraction=1.0)
    assert p.forgotten and p.deletes == [] and p.braked == []


def test_pending_add_is_adopted_or_retried():
    pending = {"alice@example.com": {"id": None, "name": "Alice Anders", "synced_at": None}}
    landed = sync.plan("office", [ALICE], [entry(4, "Alice Anders", "alice@example.com")], pending, 1.0)
    assert [(i, d.email) for i, d in landed.adoptions] == [(4, "alice@example.com")]
    lost = sync.plan("office", [ALICE], [], pending, 1.0)
    assert [d.email for d in lost.adds] == ["alice@example.com"]


def test_pending_add_with_other_entry_is_dropped():
    pending = {"alice@example.com": {"id": None, "name": "Alice Anders", "synced_at": None}}
    p = sync.plan("office", [ALICE], [entry(4, "Someone Else", "alice@example.com")], pending, 1.0)
    assert p.manual == [("alice@example.com", 4)]
    assert [e for e, _ in p.forgotten] == ["alice@example.com"]
    assert p.adds == [] and p.adoptions == []


def test_capacity_limits_adds():
    users = [User(f"u{i}@example.com", f"User {i}") for i in range(5)]
    p = sync.plan("office", users, [], {}, 1.0, capacity=10, count=8)
    assert len(p.adds) == 2 and len(p.full) == 3
    p = sync.plan("office", users, [], {}, 1.0, capacity=10, count=12)
    assert p.adds == [] and len(p.full) == 5


# -- state file --------------------------------------------------------------

def test_state_roundtrip_and_missing(tmp_path):
    path = str(tmp_path / "state.json")
    assert sync.load_state(path) is None
    sync.save_state(path, {"office": {"alice@example.com": rec(1, "Alice")}})
    assert sync.load_state(path)["office"]["alice@example.com"]["id"] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_corrupt_state_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(sync.StateError):
        sync.load_state(str(path))
    path.write_text(json.dumps(["list"]))
    with pytest.raises(sync.StateError):
        sync.load_state(str(path))


# -- end to end against the fake printer --------------------------------------

def run(printer, users, records, dry_run=False, fraction=0.3, capacity=None):
    saved = {}

    def save(r):
        saved["records"] = json.loads(json.dumps(r))

    client = RicohClient("http://192.0.2.10", "admin", "pw", http=printer)
    ok = sync.sync_printer(client, "office", users, records, dry_run=dry_run, max_delete_fraction=fraction,
                           capacity=capacity, save=save)
    return ok, saved.get("records")


def test_dry_run_writes_nothing(caplog):
    printer = FakePrinter([person("Manual Person", "manual@example.com")])
    with caplog.at_level(logging.INFO):
        ok, saved = run(printer, [ALICE, BOB], None, dry_run=True)
    assert ok and saved is None
    assert set(printer.calls) == {"startSession", "searchObjects", "getObjectsProps", "terminateSession"}
    assert printer.lock_modes == ["S"]
    assert "printer=office action=add email=alice@example.com" in caplog.text and "result=planned" in caplog.text


def test_live_first_run_then_idempotent_then_delete():
    printer = FakePrinter([person("Manual Person", "manual@example.com")])
    ok, records = run(printer, [ALICE, BOB], None)
    assert ok and printer.lock_modes == ["X"] and printer.sessions_open == 0
    assert set(records) == {"alice@example.com", "bob@example.com"}
    added = {e["mail:address"]: e for e in printer.entries.values()}
    assert added["alice@example.com"]["tagId"] == "2" and added["alice@example.com"]["isSender"] == "false"
    assert records["alice@example.com"]["id"] == int(added["alice@example.com"]["id"])

    calls_before = len(printer.calls)
    ok, records2 = run(printer, [ALICE, BOB], records)
    writes = [c for c in printer.calls[calls_before:] if c in ("putObjects", "putObjectProps", "deleteObjects")]
    assert ok and writes == [] and set(records2) == set(records)

    ok, records3 = run(printer, [ALICE], records2, fraction=0.5)
    assert ok and set(records3) == {"alice@example.com"}
    assert {e["mail:address"] for e in printer.entries.values()} == {"manual@example.com", "alice@example.com"}


def test_live_rename_updates_entry():
    printer = FakePrinter()
    _, records = run(printer, [ALICE], None)
    _, records = run(printer, [User("alice@example.com", "Alice Zimmer")], records)
    e = next(iter(printer.entries.values()))
    assert (e["name"], e["tagId"]) == ("Alice Zimmer", "2")
    assert records["alice@example.com"]["name"] == "Alice Zimmer"


def test_add_denied_stops_adds_and_keeps_state_clean():
    printer = FakePrinter()
    printer.fail["putObjects"] = fault("UDIRECTORY_PERMISSION_DENIED")
    ok, records = run(printer, [ALICE, BOB], None)
    assert not ok and records == {}
    assert printer.calls.count("putObjects") == 1
    assert printer.calls[-1] == "terminateSession"
