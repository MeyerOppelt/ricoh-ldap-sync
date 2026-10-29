import json
import logging

import pytest

from fakeprinter import FakePrinter, fault, person
from ricoh_ldap_sync import sync
from ricoh_ldap_sync.ldap_source import User
from ricoh_ldap_sync.ricoh_soap import RicohClient

NUMBERING = ["next", "lowest"]

# Interrupted-renumber tests with "retire"/"rebuild" markers cover state files
# written by 0.2.0, which copied entries before deleting the old ones.

# Emails deliberately sort differently from names.
BEA = User("z.bea@example.com", "Bea Baker")
CAL = User("a.cal@example.com", "Cal Carter")
DEE = User("m.dee@example.com", "Dee Dunn")
ABE = User("y.abe@example.com", "Abe Adams")
ZED = User("b.zed@example.com", "Zed Zane")


def run(printer, users, records, keep_sorted=True, dry_run=False, capacity=None, force=False):
    saved = {"records": records}

    def save(r):
        saved["records"] = json.loads(json.dumps(r))

    client = RicohClient("http://192.0.2.10", "admin", "pw", http=printer)
    ok = sync.sync_printer(client, "office", users, records, dry_run=dry_run, max_delete_fraction=1.0,
                           capacity=capacity, save=save, keep_sorted=keep_sorted, force_renumber=force)
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


def test_failed_re_add_leaves_rest_pending_and_next_run_finishes():
    printer = FailingAdds(allow=100)
    _, records = run(printer, [BEA, CAL, DEE], None)
    printer.allow = 2  # the new user, then one re-add, then failure
    ok, records = run(printer, [BEA, CAL, DEE, ABE], records)
    assert not ok
    pending = sorted(email for email, rec in records.items() if rec["id"] is None)
    assert len(pending) == 3  # off the panel until the next run
    assert len(printer.entries) == 1
    printer.allow = 100
    ok, records = run(printer, [BEA, CAL, DEE, ABE], records)
    assert ok
    assert owned_names_by_number(printer, records) == ["Abe Adams", "Bea Baker", "Cal Carter", "Dee Dunn"]
    assert_consistent(printer, records)


class FailingDeletes(FakePrinter):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.refuse_deletes = False

    def _deleteObjects(self, req):
        if self.refuse_deletes:
            return fault("UDIRECTORY_INTERNAL_ERROR", "busy")
        return super()._deleteObjects(req)


def test_failed_delete_restores_records_and_keeps_everyone():
    printer = FailingDeletes()
    _, records = run(printer, [BEA, CAL], None)
    printer.refuse_deletes = True
    ok, records = run(printer, [BEA, CAL, ABE], records)
    assert not ok
    assert all(rec["id"] is not None for rec in records.values())
    assert len(printer.entries) == 3
    assert_consistent(printer, records)


@pytest.mark.parametrize("numbering", NUMBERING)
def test_renumber_restarts_numbers_from_the_lowest_free(numbering):
    printer = FakePrinter(numbering=numbering)
    _, records = run(printer, [BEA, CAL, DEE], None)
    for _ in range(3):  # repeated renumbers must not push the numbers up
        newcomer = User(f"new{_}@example.com", f"Aa{_} New")
        users = [BEA, CAL, DEE, newcomer]
        ok, records = run(printer, users, records)
        assert ok
        assert sorted(int(e["index"]) for e in printer.entries.values()) == [1, 2, 3, 4]
        assert owned_names_by_number(printer, records)[0] == f"Aa{_} New"
        _, records = run(printer, [BEA, CAL, DEE], records)  # newcomer leaves again


def test_interrupted_after_delete_is_re_added_in_order():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL, DEE], None)
    # Crash after deleting, before re-adding: records are unconfirmed adds, entries gone.
    for rec in records.values():
        printer.entries.pop(rec["id"])
        rec["id"] = None
    ok, records = run(printer, [BEA, CAL, DEE], records)
    assert ok
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Cal Carter", "Dee Dunn"]
    assert_consistent(printer, records)


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


def test_renumber_works_when_the_book_is_nearly_full():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None)
    ok, records = run(printer, [BEA, CAL, ABE], records, capacity=3)
    assert ok and owned_names_by_number(printer, records) == ["Abe Adams", "Bea Baker", "Cal Carter"]


def test_sort_key_ignores_case_and_accents():
    names = ["élodie", "Zoe", "anna", "Émile", "bob"]
    assert sorted(names, key=sync.sort_key) == ["anna", "bob", "élodie", "Émile", "Zoe"]


@pytest.mark.parametrize("numbering", NUMBERING)
def test_forced_renumber_resets_numbers_of_a_sorted_list(numbering):
    # A sorted list left high by 0.2.0's copy-first renumber.
    printer = FakePrinter([dict(person(u.name, u.email), index=str(27 + n))
                           for n, u in enumerate([BEA, CAL, DEE])], numbering=numbering)
    _, records = run(printer, [BEA, CAL, DEE], None)  # adopts them
    mark = len(printer.calls)
    ok, records = run(printer, [BEA, CAL, DEE], records)
    assert ok and writes(printer, mark) == []  # sorted, so nothing to do
    ok, records = run(printer, [BEA, CAL, DEE], records, force=True, keep_sorted=False)
    assert ok
    assert sorted(int(e["index"]) for e in printer.entries.values()) == [1, 2, 3]
    assert owned_names_by_number(printer, records) == ["Bea Baker", "Cal Carter", "Dee Dunn"]
    assert_consistent(printer, records)


def test_forced_renumber_in_dry_run_writes_nothing():
    printer = FakePrinter()
    _, records = run(printer, [BEA, CAL], None)
    mark = len(printer.calls)
    ok, _ = run(printer, [BEA, CAL], records, force=True, dry_run=True)
    assert ok and writes(printer, mark) == []


# -- ordering by company -----------------------------------------------------------

def staff(email, name):
    return User(email, name)


def guest(email, name, company):
    return User(email, name, tag=company[:4], company=company)


COMPANY_USERS = [
    guest("zed@partner.example", "Zed Zane", "Acme Builders"),
    guest("amy@other.example", "Amy Allen", "Zenith Bank"),
    guest("bob@partner.example", "Bob Brown", "Acme Builders"),
    staff("yan@example.com", "Yan Young"),
    staff("cal@example.com", "Cal Carter"),
    guest("dee@third.example", "Dee Dunn", "acme architects"),  # same first 4 letters, different company
]
COMPANY_ORDER = ["Cal Carter", "Yan Young",            # no company: first, by name
                 "Dee Dunn",                           # acme architects
                 "Bob Brown", "Zed Zane",              # Acme Builders
                 "Amy Allen"]                          # Zenith Bank


@pytest.mark.parametrize("numbering", NUMBERING)
def test_entries_are_ordered_by_company_then_name(numbering):
    printer = FakePrinter(numbering=numbering)
    ok, records = run(printer, COMPANY_USERS, None)
    assert ok and owned_names_by_number(printer, records) == COMPANY_ORDER
    assert printer.calls.count("putObjects") == len(COMPANY_USERS)  # added in order, no renumber needed


@pytest.mark.parametrize("numbering", NUMBERING)
def test_name_sorted_list_is_renumbered_into_company_order(numbering):
    printer = FakePrinter(numbering=numbering)
    plain = [User(u.email, u.name) for u in COMPANY_USERS]  # as 0.5.1 left it: by name only
    _, records = run(printer, plain, None)
    assert owned_names_by_number(printer, records) == sorted(u.name for u in COMPANY_USERS)
    ok, records = run(printer, COMPANY_USERS, records)
    assert ok and owned_names_by_number(printer, records) == COMPANY_ORDER
    assert_consistent(printer, records)
    mark = len(printer.calls)
    ok, records = run(printer, COMPANY_USERS, records)
    assert ok and writes(printer, mark) == []


def test_company_change_moves_the_person():
    printer = FakePrinter()
    _, records = run(printer, COMPANY_USERS, None)
    moved = [guest(u.email, u.name, "Zenith Bank") if u.name == "Bob Brown" else u for u in COMPANY_USERS]
    ok, records = run(printer, moved, records)
    assert ok
    assert owned_names_by_number(printer, records) == ["Cal Carter", "Yan Young", "Dee Dunn", "Zed Zane",
                                                       "Amy Allen", "Bob Brown"]
    assert_consistent(printer, records)


def test_new_person_in_an_earlier_company_triggers_renumber_in_dry_run(caplog):
    printer = FakePrinter()
    _, records = run(printer, COMPANY_USERS, None)
    newcomer = guest("abe@new.example", "Abe Adams", "Acme Builders")
    with caplog.at_level(logging.INFO):
        ok, _ = run(printer, COMPANY_USERS + [newcomer], records, dry_run=True)
    assert ok and "action=renumber" in caplog.text


def test_order_key():
    assert sync.order_key(sync.desired(User("a@example.com", "Émile Zola", company="Éclair Co"))) == \
        ("eclair co", "emile zola", "a@example.com")
    assert sync.order_key(sync.desired(User("b@example.com", "Bob"))) == ("", "bob", "b@example.com")
