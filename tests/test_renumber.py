import json
import logging

import pytest

from fakeprinter import FakePrinter, fault, person
from ricoh_ldap_sync import sync
from ricoh_ldap_sync.ldap_source import User
from ricoh_ldap_sync.ricoh_soap import RicohClient

NUMBERING = ["next", "lowest"]

# Emails deliberately sort differently from names.
BEA = User("z.bea@example.com", "Bea Baker")
CAL = User("a.cal@example.com", "Cal Carter")
DEE = User("m.dee@example.com", "Dee Dunn")
ABE = User("y.abe@example.com", "Abe Adams")
ZED = User("b.zed@example.com", "Zed Zane")


def run(printer, users, records, keep_sorted=True, dry_run=False, capacity=None):
    saved = {"records": records}

    def save(r):
        saved["records"] = json.loads(json.dumps(r))

    client = RicohClient("http://192.0.2.10", "admin", "pw", http=printer)
    ok = sync.sync_printer(client, "office", users, records, dry_run=dry_run, max_delete_fraction=1.0,
                           capacity=capacity, save=save, keep_sorted=keep_sorted)
    return ok, saved["records"]


def owned_names_by_number(printer, records):
    ids = {r["id"] for r in records.values()}
    return [e["name"] for e in sorted(printer.entries.values(), key=lambda e: int(e["index"])) if int(e["id"]) in ids]


def writes(printer, since=0):
    return [c for c in printer.calls[since:] if c in ("putObjects", "putObjectProps", "deleteObjects")]


def assert_consistent(printer, records):
    """State matches the printer: one entry per owned email, no flags, no duplicates."""
    for email, rec in records.items():
        assert "rebuild" not in rec and "retire" not in rec
        assert printer.entries[rec["id"]]["mail:address"] == email
    emails = [e["mail:address"] for e in printer.entries.values()]
    assert len(emails) == len(set(emails))


@pytest.mark.parametrize("numbering", NUMBERING)
def test_first_batch_is_added_in_name_order(numbering):
    printer = FakePrinter(numbering=numbering)
    ok, records = run(printer, [DEE, BEA, CAL], None)
    assert ok
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Cal Carter", "Dee Dunn"]
    assert printer.calls.count("putObjects") == 3  # no renumber needed


@pytest.mark.parametrize("numbering", NUMBERING)
def test_new_user_sorting_first_triggers_renumber(numbering):
    printer = FakePrinter([person("Manual Person", "manual@example.com")], numbering=numbering)
    manual_before = dict(printer.entries[1])
    _, records = run(printer, [BEA, CAL, DEE], None)
    ok, records = run(printer, [BEA, CAL, DEE, ABE], records)
    assert ok
    assert owned_names_by_number(printer, records) == ["Abe Adams", "Bea Baker", "Cal Carter", "Dee Dunn"]
    assert printer.entries[1] == manual_before  # hand-made entry untouched
    assert_consistent(printer, records)
    assert printer.sessions_open == 0


@pytest.mark.parametrize("numbering", NUMBERING)
def test_new_user_sorting_last_needs_no_renumber(numbering):
    printer = FakePrinter(numbering=numbering)
    _, records = run(printer, [BEA, CAL], None)
    mark = len(printer.calls)
    ok, records = run(printer, [BEA, CAL, ZED], records)
    assert ok and writes(printer, mark) == ["putObjects"]
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Cal Carter", "Zed Zane"]


@pytest.mark.parametrize("numbering", NUMBERING)
def test_removal_keeps_order_without_renumber(numbering):
    printer = FakePrinter(numbering=numbering)
    _, records = run(printer, [BEA, CAL, DEE], None)
    mark = len(printer.calls)
    ok, records = run(printer, [BEA, DEE], records)
    assert ok and writes(printer, mark) == ["deleteObjects"]
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Dee Dunn"]


@pytest.mark.parametrize("numbering", NUMBERING)
def test_rename_that_moves_position_triggers_renumber(numbering):
    printer = FakePrinter(numbering=numbering)
    _, records = run(printer, [BEA, CAL, DEE], None)
    renamed = User(DEE.email, "Aaron Dunn")
    ok, records = run(printer, [BEA, CAL, renamed], records)
    assert ok
    assert owned_names_by_number(printer, records) == ["Aaron Dunn", "Bea Baker", "Cal Carter"]
    assert_consistent(printer, records)


def test_keep_sorted_off_never_renumbers():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None, keep_sorted=False)
    mark = len(printer.calls)
    ok, records = run(printer, [BEA, CAL, ABE], records, keep_sorted=False)
    assert ok and writes(printer, mark) == ["putObjects"]
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Cal Carter", "Abe Adams"]


def test_dry_run_reports_renumber_without_writing(caplog):
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None)
    mark = len(printer.calls)
    with caplog.at_level(logging.INFO):
        ok, _ = run(printer, [BEA, CAL, ABE], records, dry_run=True)
    assert ok and writes(printer, mark) == []
    assert "action=renumber email=* entries=3 result=planned" in caplog.text


class FailingAdds(FakePrinter):
    """Refuses putObjects after a number of successful ones."""

    def __init__(self, allow, **kw):
        super().__init__(**kw)
        self.allow = allow

    def _putObjects(self, req):
        if self.allow <= 0:
            return fault("UDIRECTORY_INTERNAL_ERROR", "out of memory")
        self.allow -= 1
        return super()._putObjects(req)


def test_failed_renumber_rolls_back_and_keeps_everyone():
    printer = FailingAdds(allow=100)
    _, records = run(printer, [BEA, CAL, DEE], None)
    printer.allow = 2  # the new user, then one copy, then failure
    ok, records = run(printer, [BEA, CAL, DEE, ABE], records)
    assert not ok
    emails = sorted(e["mail:address"] for e in printer.entries.values())
    assert emails == sorted(u.email for u in (ABE, BEA, CAL, DEE))  # nobody lost, no duplicates
    assert_consistent(printer, records)
    printer.allow = 100
    ok, records = run(printer, [BEA, CAL, DEE, ABE], records)
    assert ok and owned_names_by_number(printer, records) == ["Abe Adams", "Bea Baker", "Cal Carter", "Dee Dunn"]


@pytest.mark.parametrize("numbering", NUMBERING)
def test_interrupted_after_copies_recorded_is_finished_next_run(numbering):
    printer = FakePrinter(numbering=numbering)
    _, records = run(printer, [BEA, CAL], None)
    # Simulate a crash after the copies were recorded but before old entries were deleted.
    for rec in records.values():
        old = rec["id"]
        new = printer.insert({k: v for k, v in printer.entries[old].items() if k not in ("id", "index")})
        rec.update(id=new, retire=[old], rebuild=True)
    ok, records = run(printer, [BEA, CAL], records)
    assert ok and len(printer.entries) == 2
    assert_consistent(printer, records)


def test_interrupted_while_copying_removes_unrecorded_copies():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None)
    # Crash while copying: flag set, one unrecorded copy exists.
    for rec in records.values():
        rec["rebuild"] = True
    old = records[BEA.email]["id"]
    printer.insert({k: v for k, v in printer.entries[old].items() if k not in ("id", "index")})
    ok, records = run(printer, [BEA, CAL], records)
    assert ok and len(printer.entries) == 2
    assert records[BEA.email]["id"] == old
    assert_consistent(printer, records)


def test_capacity_too_small_for_copies_skips_renumber():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None)
    mark = len(printer.calls)
    ok, records = run(printer, [BEA, CAL, ABE], records, capacity=4)
    assert not ok and writes(printer, mark) == ["putObjects"]
    assert len(printer.entries) == 3


def test_sort_key_ignores_case_and_accents():
    names = ["élodie", "Zoe", "anna", "Émile", "bob"]
    assert sorted(names, key=sync.sort_key) == ["anna", "bob", "élodie", "Émile", "Zoe"]
