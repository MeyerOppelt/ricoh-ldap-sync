import json
import logging

import pytest

from fakeprinter import FakePrinter, person
from ricoh_ldap_sync import config, ldap_source, sync
from ricoh_ldap_sync.ldap_source import User
from ricoh_ldap_sync.ricoh_soap import Entry, RicohClient

ENV = {
    "LDAP_URL": "ldaps://ldap.example.com",
    "LDAP_BIND_DN": "uid=sync,ou=people,dc=example,dc=com",
    "LDAP_BIND_PASSWORD": "pw",
    "LDAP_BASE": "ou=people,dc=example,dc=com",
    "PRINTERS": '[{"name":"office","host":"192.0.2.10"}]',
    "RICOH_PASSWORD": "pw",
}


def cfg(**extra):
    return config.load({**ENV, **extra})


# -- configuration -------------------------------------------------------------

def test_company_off_by_default():
    c = cfg()
    assert c.ldap_attr_company == "" and c.company_tags == {}


def test_company_tags_parsed():
    c = cfg(LDAP_ATTR_COMPANY="company", COMPANY_TAGS='{"Example  Holdings": " EH ", "Other Co": "OC"}')
    assert c.company_tags == {"Example Holdings": "EH", "Other Co": "OC"}


@pytest.mark.parametrize("raw, fragment", [
    ("not json", "not valid JSON"),
    ('["EH"]', "JSON object"),
    ('{"Example": 3}', "JSON object"),
    ('{"Example": ""}', "1 to 6"),
    ('{"Example": "TOOLONG"}', "1 to 6"),
])
def test_bad_company_tags(raw, fragment):
    with pytest.raises(config.ConfigError) as e:
        cfg(COMPANY_TAGS=raw)
    assert any("COMPANY_TAGS" in p and fragment in p for p in e.value.problems)


# -- reading LDAP --------------------------------------------------------------

@pytest.mark.parametrize("company, tag", [
    ("Example Holdings", "EH"),            # mapped
    ("example holdings", "EH"),            # case-insensitive
    ("Northwind Traders Inc", "Nort"),     # first 4 characters when unmapped
    ("Contoso", "Cont"),
    ("ACME", "ACME"),
    ("Oak", "Oak"),                        # shorter than 4
    ("VC Interiors", "VC I"),
    ("Ab  Cd", "Ab C"),                    # runs of spaces collapse first
    ("Ab Cdef", "Ab C"),
    ("", ""),
    ("   ", ""),
])
def test_company_tag(company, tag):
    assert ldap_source.company_tag(company, {"Example Holdings": "EH"}) == tag


def test_to_users_reads_company_when_configured():
    c = cfg(LDAP_ATTR_COMPANY="company", COMPANY_TAGS='{"Example Holdings": "EH"}')
    users = ldap_source.to_users([
        {"displayName": ["Alice Anders"], "mail": ["alice@example.com"], "company": ["Example Holdings"]},
        {"displayName": ["Bob Brown"], "mail": ["bob@example.com"]},
    ], c)
    assert [(u.email, u.tag) for u in users] == [("alice@example.com", "EH"), ("bob@example.com", "")]


def test_to_users_ignores_company_when_not_configured():
    users = ldap_source.to_users([{"cn": ["Alice"], "mail": ["alice@example.com"], "company": ["Example"]}], cfg())
    assert users[0].tag == ""


@pytest.mark.parametrize("mail", ["null", "None", "not an address", "a@b", "two@@example.com", "a b@example.com"])
def test_non_addresses_are_skipped(mail, caplog):
    with caplog.at_level(logging.WARNING):
        users = ldap_source.to_users([{"cn": ["Verum Admin"], "mail": [mail]}], cfg())
    assert users == [] and "is not an email address" in caplog.text


def test_search_requests_company_attribute():
    from ldap3 import MOCK_SYNC, Connection, Server
    conn = Connection(Server("mock"), user="uid=sync,ou=people,dc=example,dc=com", password="pw", client_strategy=MOCK_SYNC)
    conn.strategy.add_entry("uid=sync,ou=people,dc=example,dc=com", {"uid": "sync", "userPassword": "pw"})
    conn.strategy.add_entry("uid=alice,ou=people,dc=example,dc=com",
                            {"uid": "alice", "displayName": "Alice Anders", "mail": "alice@example.com",
                             "company": "Example Holdings"})
    conn.bind()
    c = cfg(LDAP_ATTR_COMPANY="company")
    users = ldap_source.to_users(ldap_source.search(conn, c), c)
    assert [(u.email, u.tag) for u in users] == [("alice@example.com", "Exam")]


# -- the button label ----------------------------------------------------------

@pytest.mark.parametrize("name, tag, label", [
    ("Alice Anders", "", "Alice Anders"),
    ("Carley Goetschius", "", "Carley Goetschiu"),          # unchanged behaviour without a tag
    ("Alice Anders", "EH", "Alice A. (EH)"),                # "Alice Anders (EH)" is 17 characters
    ("Bob Brown", "EH", "Bob Brown (EH)"),
    ("Carley Goetschius", "VCI", "Carley G. (VCI)"),
    ("Accounting Veldhouse", "VC", "Accounting (VC)"),      # "Accounting V." doesn't fit either
    ("Blacktie", "VC", "Blacktie (VC)"),
    ("Maximiliano Fitzgerald-Montgomery", "NTI", "Maximilian (NTI)"),
    ("Alice Anders", "Veld", "Alice A. (Veld)"),
    ("Carley Goetschius", "Veld", "Carley G. (Veld)"),
])
def test_key_display(name, tag, label):
    out = sync.key_display(name, tag)
    assert out == label and len(out) <= sync.KEY_DISPLAY_MAX


def test_name_field_keeps_full_name():
    d = sync.desired(User("carley@example.com", "Carley Goetschius", tag="VCI"))
    assert d.name == "Carley Goetschius" and d.key_display == "Carley G. (VCI)"
    assert dict(d.add_props())["longName"] == "Carley G. (VCI)"


# -- planning and applying -----------------------------------------------------

def entry(entry_id, name, email, long_name=None):
    return Entry(entry_id, {"id": str(entry_id), "name": name, "longName": long_name if long_name is not None else name[:16],
                            "mail:": "true", "mail:address": email})


def test_old_records_without_label_cause_no_rewrite():
    records = {"bob@example.com": {"id": 1, "name": "Bob Brown", "synced_at": "x"}}
    p = sync.plan("office", [User("bob@example.com", "Bob Brown")], [entry(1, "Bob Brown", "bob@example.com")],
                  records, 1.0)
    assert p.updates == [] and p.unchanged == ["bob@example.com"]


def test_new_company_updates_label():
    records = {"bob@example.com": {"id": 1, "name": "Bob Brown", "synced_at": "x"}}
    p = sync.plan("office", [User("bob@example.com", "Bob Brown", tag="EH")], [entry(1, "Bob Brown", "bob@example.com")],
                  records, 1.0)
    assert [(i, d.key_display) for i, d, _ in p.updates] == [(1, "Bob Brown (EH)")]


def test_adoption_fixes_label():
    p = sync.plan("office", [User("bob@example.com", "Bob Brown", tag="EH")],
                  [entry(1, "Bob Brown", "bob@example.com", long_name="Bob")], None, 1.0)
    assert [(i, d.email) for i, d in p.adoptions] == [(1, "bob@example.com")]
    assert [(i, d.key_display) for i, d, _ in p.updates] == [(1, "Bob Brown (EH)")]


def run(printer, users, records, **kw):
    saved = {"records": records}

    def save(r):
        saved["records"] = json.loads(json.dumps(r))

    client = RicohClient("http://192.0.2.10", "admin", "pw", http=printer)
    opts = dict(dry_run=False, max_delete_fraction=1.0, capacity=None, save=save)
    opts.update(kw)
    ok = sync.sync_printer(client, "office", users, records, **opts)
    return ok, saved["records"]


def test_live_company_change_relabels_entry():
    printer = FakePrinter()
    _, records = run(printer, [User("bob@example.com", "Bob Brown", tag="EH")], None)
    entry_ = next(iter(printer.entries.values()))
    assert entry_["longName"] == "Bob Brown (EH)" and entry_["name"] == "Bob Brown"
    assert records["bob@example.com"]["key_display"] == "Bob Brown (EH)"
    ok, records = run(printer, [User("bob@example.com", "Bob Brown", tag="OC")], records)
    assert ok and next(iter(printer.entries.values()))["longName"] == "Bob Brown (OC)"
    mark = len(printer.calls)
    ok, records = run(printer, [User("bob@example.com", "Bob Brown", tag="OC")], records)
    assert ok and not [c for c in printer.calls[mark:] if c.startswith("put")]


# -- safety brake reporting ----------------------------------------------------

def test_braked_run_counts_as_failed_with_one_summary_line(caplog):
    printer = FakePrinter()
    _, records = run(printer, [User("a@example.com", "Ann"), User("b@example.com", "Ben")], None)
    with caplog.at_level(logging.INFO):
        ok, records = run(printer, [], records)
    assert not ok and len(printer.entries) == 2
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "LDAP returned 0 users" in errors[0].getMessage()


def test_braked_dry_run_also_counts_as_failed():
    printer = FakePrinter()
    _, records = run(printer, [User("a@example.com", "Ann")], None)
    ok, _ = run(printer, [], records, dry_run=True)
    assert not ok


def test_fraction_brake_still_lists_each_blocked_delete(caplog):
    printer = FakePrinter()
    users = [User(f"u{i}@example.com", f"User {i}") for i in range(4)]
    _, records = run(printer, users, None)
    with caplog.at_level(logging.INFO):
        ok, _ = run(printer, users[:1], records, max_delete_fraction=0.3)
    assert not ok and caplog.text.count("blocked by safety brake") == 3


# -- guest addresses -----------------------------------------------------------

@pytest.mark.parametrize("upn, address", [
    ("brad_partner.example#EXT#@tenant.onmicrosoft.com", "brad@partner.example"),
    ("Brad_Partner.Example#EXT#@Tenant.onmicrosoft.com", "brad@partner.example"),
    ("first_last_partner.example#EXT#@tenant.onmicrosoft.com", "first_last@partner.example"),
    ("first.last_sub.partner.example#ext#@tenant.onmicrosoft.com", "first.last@sub.partner.example"),
    ("nounderscore#EXT#@tenant.onmicrosoft.com", ""),
    ("brad_nodot#EXT#@tenant.onmicrosoft.com", ""),
])
def test_guest_address(upn, address):
    assert ldap_source.guest_address(upn) == address


def test_guest_upn_in_mail_becomes_real_address(caplog):
    with caplog.at_level(logging.INFO):
        users = ldap_source.to_users([
            {"displayName": ["Brad Walle"], "mail": ["brad_partner.example#EXT#@tenant.onmicrosoft.com"]},
            {"displayName": ["Gus Guest"], "mail": ["gus@partner.example"]},
        ], cfg())
    assert [u.email for u in users] == ["brad@partner.example", "gus@partner.example"]
    assert "using the guest's real address brad@partner.example" in caplog.text


def test_unrecoverable_guest_upn_is_skipped(caplog):
    with caplog.at_level(logging.WARNING):
        users = ldap_source.to_users([{"cn": ["Odd Guest"], "mail": ["odd#EXT#@tenant.onmicrosoft.com"]}], cfg())
    assert users == [] and "no recoverable address" in caplog.text


def test_guest_upn_and_real_address_are_the_same_person():
    users = ldap_source.to_users([
        {"cn": ["Brad Walle"], "mail": ["brad@partner.example"]},
        {"cn": ["Brad Walle"], "mail": ["brad_partner.example#EXT#@tenant.onmicrosoft.com"]},
    ], cfg())
    assert [u.email for u in users] == ["brad@partner.example"]
