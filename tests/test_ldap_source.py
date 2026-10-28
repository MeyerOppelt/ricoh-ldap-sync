import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from ricoh_ldap_sync import config, ldap_source

ENV = {
    "LDAP_URL": "ldaps://ldap.example.com",
    "LDAP_BIND_DN": "uid=sync,ou=people,dc=example,dc=com",
    "LDAP_BIND_PASSWORD": "pw",
    "LDAP_BASE": "ou=people,dc=example,dc=com",
    "PRINTERS": '[{"name":"office","host":"192.0.2.10"}]',
    "RICOH_PASSWORD": "pw",
}


@pytest.fixture
def cfg():
    return config.load(ENV)


def test_name_fallback_and_lowercase_email(cfg):
    users = ldap_source.to_users([
        {"uid": ["alice"], "cn": ["Alice A"], "displayName": ["Alice Anders"], "mail": ["Alice@Example.com"]},
        {"uid": ["bob"], "cn": ["Bob B"], "displayName": [], "mail": ["bob@example.com"]},
        {"uid": ["carol"], "mail": ["carol@example.com"]},
    ], cfg)
    assert [(u.email, u.name) for u in users] == [
        ("alice@example.com", "Alice Anders"),
        ("bob@example.com", "Bob B"),
        ("carol@example.com", "carol"),
    ]


def test_skips_incomplete_and_duplicate(cfg):
    users = ldap_source.to_users([
        {"uid": ["nomail"], "cn": ["No Mail"]},
        {"mail": ["noname@example.com"]},
        {"uid": ["dave"], "mail": ["dave@example.com"]},
        {"uid": ["dave2"], "mail": ["DAVE@example.com"]},
    ], cfg)
    assert [(u.email, u.name) for u in users] == [("dave@example.com", "dave")]


def test_attribute_names_case_insensitive(cfg):
    users = ldap_source.to_users([{"DISPLAYNAME": ["Eve E"], "Mail": ["eve@example.com"]}], cfg)
    assert users[0].name == "Eve E"


def mock_connection():
    conn = Connection(Server("mock"), user="uid=sync,ou=people,dc=example,dc=com", password="pw", client_strategy=MOCK_SYNC)
    conn.strategy.add_entry("uid=sync,ou=people,dc=example,dc=com", {"uid": "sync", "userPassword": "pw", "objectClass": "person"})
    conn.strategy.add_entry("uid=alice,ou=people,dc=example,dc=com",
                            {"uid": "alice", "cn": "Alice A", "displayName": "Alice Anders", "mail": "alice@example.com", "objectClass": "person"})
    conn.strategy.add_entry("uid=bob,ou=people,dc=example,dc=com",
                            {"uid": "bob", "cn": "Bob B", "mail": "bob@example.com", "objectClass": "person"})
    conn.strategy.add_entry("uid=nomail,ou=people,dc=example,dc=com", {"uid": "nomail", "cn": "No Mail", "objectClass": "person"})
    assert conn.bind()
    return conn


def test_search_against_mock_directory(cfg):
    conn = mock_connection()
    users = ldap_source.to_users(ldap_source.search(conn, cfg), cfg)
    assert [(u.email, u.name) for u in users] == [("alice@example.com", "Alice Anders"), ("bob@example.com", "Bob B")]


def test_search_error_raises_not_empty(cfg, monkeypatch):
    conn = mock_connection()

    def fail(*a, **k):
        conn.result = {"result": 4, "description": "sizeLimitExceeded", "message": ""}
        return False

    monkeypatch.setattr(conn, "search", fail)
    with pytest.raises(ldap_source.LdapError, match="sizeLimitExceeded"):
        ldap_source.search(conn, cfg)


def test_tls_validates_configured_name():
    cfg = config.load({**ENV, "LDAP_URL": "ldaps://ldap-container:13389", "LDAP_TLS_NAME": "ldap.example.com"})
    server = ldap_source.make_server(cfg)
    assert (server.host, server.port, server.ssl) == ("ldap-container", 13389, True)
    assert server.tls.sni == "ldap.example.com"
    assert server.tls.valid_names == ["ldap.example.com"]
