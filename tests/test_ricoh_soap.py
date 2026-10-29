import base64

import pytest
from lxml import etree

from fakeprinter import FakePrinter, fault, person
from ricoh_ldap_sync import ricoh_soap
from ricoh_ldap_sync.ricoh_soap import RicohClient, RicohError

ENV = ricoh_soap.NS_ENV
UDIR = ricoh_soap.NS_UDIR


def client(printer, **kw):
    return RicohClient("http://192.0.2.10", "admin", "pa$$word", http=printer, **kw)


def body(raw):
    return etree.fromstring(raw).find(f"{{{ENV}}}Body")[0]


def test_session_credentials_and_lock_mode():
    printer = FakePrinter()
    c = client(printer)
    with c.session(write=False):
        assert c.session_id == "4242"
    with c.session(write=True):
        pass
    assert printer.lock_modes == ["S", "X"]
    assert printer.sessions_open == 0
    start = body(printer.requests[0])
    assert etree.QName(start).localname == "startSession" and etree.QName(start).namespace == UDIR
    fields = dict(part.split("=", 1) for part in start.findtext("stringIn").split(";") if "=" in part)
    assert base64.b64decode(fields["SCHEME"]) == b"BASIC"
    assert base64.b64decode(fields["UID:UserName"]) == b"admin"
    assert base64.b64decode(fields["PWD:Password"]) == b"pa$$word"
    assert start.findtext("timeLimit") == "30"


def test_soap_action_and_endpoint():
    seen = {}

    class Recorder(FakePrinter):
        def post(self, url, data, headers, timeout):
            seen.update(url=url, headers=headers)
            return super().post(url, data, headers, timeout)

    c = client(Recorder())
    c.start_session(write=False)
    assert seen["url"] == "http://192.0.2.10/DH/udirectory"
    assert seen["headers"]["SOAPAction"] == "http://www.ricoh.co.jp/xmlns/soap/rdh/udirectory#startSession"


def test_proxy_only_on_this_session():
    printer = FakePrinter()
    client(printer, proxy="http://localhost:1055")
    assert printer.trust_env is False
    assert printer.proxies == {"http": "http://localhost:1055", "https": "http://localhost:1055"}


def test_session_closed_after_error():
    printer = FakePrinter()
    printer.fail["searchObjects"] = fault("UDIRECTORY_INTERNAL_ERROR", "boom")
    c = client(printer)
    with pytest.raises(RicohError, match="boom"):
        with c.session(write=False):
            c.search_ids()
    assert printer.calls[-1] == "terminateSession"
    assert printer.sessions_open == 0


def test_search_paginates():
    printer = FakePrinter([person(f"User {i}", f"user{i}@example.com") for i in range(120)])
    c = client(printer)
    with c.session(write=False):
        ids, total = c.search_ids()
    assert total == 120 and ids == list(range(1, 121))
    assert printer.calls.count("searchObjects") == 3


def test_search_skips_long_ids():
    printer = FakePrinter([person("Alice", "alice@example.com"), {"id": "1234567890", "name": "special"}])
    c = client(printer)
    with c.session(write=False):
        ids, total = c.search_ids()
    assert ids == [1] and total == 2


def test_get_entries_batches_and_parses():
    printer = FakePrinter([person(f"User {i}", f"User{i}@Example.com") for i in range(60)])
    c = client(printer)
    with c.session(write=False):
        entries = c.get_entries(range(1, 61))
    assert len(entries) == 60
    assert printer.calls.count("getObjectsProps") == 2
    e = entries[0]
    assert (e.id, e.name, e.email, e.tag_ids) == (1, "User 0", "user0@example.com", (2,))
    select = [i.text for i in body(printer.requests[1]).find("selectProps")]
    assert "mail:address" in select and not any("password" in s for s in select)


def test_entry_without_mail_flag_has_no_email():
    e = ricoh_soap.Entry(1, {"mail:": "false", "mail:address": "old@example.com"})
    assert e.email == ""


def test_writes():
    printer = FakePrinter([person("Alice", "alice@example.com")])
    c = client(printer)
    with c.session(write=True):
        c.add_entry([("name", "Bob"), ("mail:", "true"), ("mail:address", "bob@example.com")])
        c.update_entry(1, [("name", "Alice A")])
        c.delete_entries([1])
    assert list(printer.entries.values())[0]["name"] == "Bob"
    put = body(printer.requests[1])
    assert put.findtext("objectClass") == "entry"
    lists = put.find("propListList")
    assert lists.get(f"{{{ricoh_soap.NS_ENC}}}arrayType") == "ricoh:propertyList[]"
    assert lists.get(f"{{{ricoh_soap.NS_XSI}}}type") == "soap-enc:Array"
    assert body(printer.requests[2]).findtext("objectId") == "entry:1"
    assert [i.text for i in body(printer.requests[3]).find("objectIdList")] == ["entry:1"]


def test_fault_carries_code():
    printer = FakePrinter()
    printer.fail["putObjects"] = fault("UDIRECTORY_PERMISSION_DENIED")
    c = client(printer)
    with c.session(write=True):
        with pytest.raises(RicohError) as e:
            c.add_entry([("name", "x")])
    assert e.value.code == "UDIRECTORY_PERMISSION_DENIED"
    assert "putObjects" in str(e.value)


def test_transport_error_and_non_xml():
    class Down(FakePrinter):
        def post(self, *a, **k):
            import requests
            raise requests.ConnectTimeout("timed out")

    with pytest.raises(RicohError, match="ConnectTimeout"):
        client(Down()).start_session(write=False)

    class Html(FakePrinter):
        def post(self, *a, **k):
            from fakeprinter import Response
            return Response("<html><body>Login", 200)

    with pytest.raises(RicohError, match="not XML"):
        client(Html()).start_session(write=False)


def test_password_not_in_repr_or_errors():
    printer = FakePrinter()
    printer.fail["startSession"] = fault("UDIRECTORY_AUTHENTICATION_FAILED")
    c = client(printer)
    with pytest.raises(RicohError) as e:
        c.start_session(write=False)
    assert "pa$$word" not in str(e.value) and "pa$$word" not in repr(c.__dict__.get("url"))
