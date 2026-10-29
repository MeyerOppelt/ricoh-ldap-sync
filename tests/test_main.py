import json

import pytest

from fakeprinter import FakePrinter, person
from ricoh_ldap_sync import config, ldap_source, main
from ricoh_ldap_sync.ldap_source import User
from ricoh_ldap_sync.ricoh_soap import RicohClient

ENV = {
    "LDAP_URL": "ldaps://ldap.example.com",
    "LDAP_BIND_DN": "uid=sync,ou=people,dc=example,dc=com",
    "LDAP_BIND_PASSWORD": "pw",
    "LDAP_BASE": "ou=people,dc=example,dc=com",
    "PRINTERS": '[{"name":"office","host":"192.0.2.10"},{"name":"lab","host":"192.0.2.11"}]',
    "RICOH_PASSWORD": "pw",
    "DRY_RUN": "false",
}
USERS = [User("alice@example.com", "Alice Anders"), User("bob@example.com", "Bob Brown")]


@pytest.fixture
def printers(monkeypatch):
    fakes = {"http://192.0.2.10": FakePrinter([person("Manual Person", "manual@example.com")]),
             "http://192.0.2.11": FakePrinter()}

    def make(base_url, user, password, proxy=None):
        return RicohClient(base_url, user, password, proxy=proxy, http=fakes[base_url])

    monkeypatch.setattr(main, "RicohClient", make)
    return fakes


def users(monkeypatch, result):
    def fetch(cfg):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(ldap_source, "fetch_users", fetch)


def test_ldap_error_skips_every_printer(monkeypatch, printers, tmp_path):
    users(monkeypatch, ldap_source.LdapError("connection refused"))
    assert main.run_once(config.load(ENV), str(tmp_path / "state.json")) is False
    assert all(p.calls == [] for p in printers.values())
    assert not (tmp_path / "state.json").exists()


def test_first_live_run_creates_state_for_each_printer(monkeypatch, printers, tmp_path):
    users(monkeypatch, USERS)
    path = tmp_path / "state.json"
    assert main.run_once(config.load(ENV), str(path)) is True
    state = json.loads(path.read_text())
    assert set(state) == {"office", "lab"}
    assert set(state["office"]) == {"alice@example.com", "bob@example.com"}
    assert all(r["id"] is not None for r in state["lab"].values())


def test_one_printer_failing_does_not_block_the_other(monkeypatch, printers, tmp_path):
    users(monkeypatch, USERS)

    class Down(FakePrinter):
        def post(self, *a, **k):
            import requests
            raise requests.ConnectTimeout("no route")

    printers["http://192.0.2.10"] = Down()
    path = tmp_path / "state.json"
    assert main.run_once(config.load(ENV), str(path)) is False
    assert set(json.loads(path.read_text())) == {"lab"}


def test_dry_run_writes_no_state(monkeypatch, printers, tmp_path):
    users(monkeypatch, USERS)
    path = tmp_path / "state.json"
    assert main.run_once(config.load({**ENV, "DRY_RUN": "true"}), str(path)) is True
    assert not path.exists()
    assert all("putObjects" not in p.calls for p in printers.values())


def test_corrupt_state_skips_run(monkeypatch, printers, tmp_path):
    users(monkeypatch, USERS)
    path = tmp_path / "state.json"
    path.write_text("{oops")
    assert main.run_once(config.load(ENV), str(path)) is False
    assert all(p.calls == [] for p in printers.values())
    assert path.read_text() == "{oops"
