import pytest

from ricoh_ldap_sync import config

BASE = {
    "LDAP_URL": "ldaps://ldap.example.com:636",
    "LDAP_BIND_DN": "uid=sync,ou=people,dc=example,dc=com",
    "LDAP_BIND_PASSWORD": "secret-ldap",
    "LDAP_BASE": "ou=people,dc=example,dc=com",
    "PRINTERS": '[{"name":"office-1","host":"192.0.2.10"}]',
    "RICOH_PASSWORD": "secret-printer",
}


def load(**overrides):
    env = {**BASE, **overrides}
    return config.load({k: v for k, v in env.items() if v is not None})


def problems(**overrides):
    with pytest.raises(config.ConfigError) as e:
        load(**overrides)
    return e.value.problems


def test_defaults():
    cfg = load()
    assert cfg.ldap_host == "ldap.example.com"
    assert cfg.ldap_port == 636
    assert cfg.ldap_use_ssl
    assert cfg.ldap_tls_name == "ldap.example.com"
    assert cfg.ldap_filter == "(&(uid=*)(mail=*))"
    assert cfg.ldap_attr_name == ("displayName", "cn", "uid")
    assert cfg.ldap_attr_mail == "mail"
    assert cfg.ricoh_user == "admin"
    assert cfg.dry_run is True
    assert cfg.sync_interval_minutes == 15
    assert cfg.max_delete_fraction == 0.3
    assert cfg.log_level == "info"
    assert cfg.tailscale_enabled is False
    assert cfg.printer_proxy is None
    assert cfg.ts_hostname == "ricoh-ldap-sync"
    assert cfg.ts_login_server is None
    assert [(p.name, p.host, p.password) for p in cfg.printers] == [("office-1", "192.0.2.10", "secret-printer")]


def test_tls_name_override_and_default_ports():
    cfg = load(LDAP_URL="ldaps://ldap-container:13389", LDAP_TLS_NAME="ldap.example.com")
    assert (cfg.ldap_host, cfg.ldap_port, cfg.ldap_tls_name) == ("ldap-container", 13389, "ldap.example.com")
    assert load(LDAP_URL="ldaps://ldap.example.com").ldap_port == 636
    plain = load(LDAP_URL="ldap://ldap.example.com")
    assert (plain.ldap_port, plain.ldap_use_ssl) == (389, False)


def test_tailscale_enables_proxy():
    cfg = load(TS_AUTHKEY="tskey-x", TS_LOGIN_SERVER="https://headscale.example.com")
    assert cfg.tailscale_enabled
    assert cfg.printer_proxy == "http://localhost:1055"
    assert cfg.ts_login_server == "https://headscale.example.com"


def test_headscale_key_requires_login_server():
    assert any("TS_LOGIN_SERVER" in p for p in problems(TS_AUTHKEY="hskey-auth-x"))
    assert load(TS_AUTHKEY="hskey-auth-x", TS_LOGIN_SERVER="https://headscale.example.com").tailscale_enabled


def test_per_printer_password_override():
    cfg = load(
        PRINTERS='[{"name":"office-1","host":"192.0.2.10"},{"name":"lab","host":"printer.example.com:8080"}]',
        RICOH_PASSWORD_LAB="lab-secret",
    )
    assert [p.password for p in cfg.printers] == ["secret-printer", "lab-secret"]
    assert cfg.printers[1].base_url == "http://printer.example.com:8080"
    assert config.password_var("office-1") == "RICOH_PASSWORD_OFFICE_1"


def test_default_password_optional_when_every_printer_has_override():
    cfg = load(RICOH_PASSWORD=None, RICOH_PASSWORD_OFFICE_1="only")
    assert cfg.printers[0].password == "only"


def test_missing_required_all_reported():
    p = problems(LDAP_URL=None, LDAP_BIND_DN=None, LDAP_BIND_PASSWORD=None, LDAP_BASE=None, PRINTERS=None)
    joined = "\n".join(p)
    for var in ("LDAP_URL", "LDAP_BIND_DN", "LDAP_BIND_PASSWORD", "LDAP_BASE", "PRINTERS"):
        assert var in joined


def test_missing_printer_password():
    assert any("RICOH_PASSWORD" in p for p in problems(RICOH_PASSWORD=None))


@pytest.mark.parametrize("printers, fragment", [
    ("not json", "not valid JSON"),
    ("[]", "non-empty JSON list"),
    ('{"name":"a","host":"b"}', "non-empty JSON list"),
    ('["office"]', "must be an object"),
    ('[{"name":"bad name","host":"192.0.2.10"}]', ".name"),
    ('[{"name":"a","host":"http://192.0.2.10"}]', ".host"),
    ('[{"name":"a"}]', ".host"),
    ('[{"name":"a-b","host":"192.0.2.10"},{"name":"A_B","host":"192.0.2.11"}]', "more than one printer"),
])
def test_bad_printers(printers, fragment):
    assert any(fragment in p for p in problems(PRINTERS=printers))


@pytest.mark.parametrize("var, value", [
    ("LDAP_URL", "https://ldap.example.com"),
    ("LDAP_URL", "ldaps://"),
    ("LDAP_URL", "ldaps://ldap.example.com:notaport"),
    ("LDAP_FILTER", "uid=*"),
    ("LDAP_ATTR_NAME", " , "),
    ("SYNC_INTERVAL_MINUTES", "0"),
    ("SYNC_INTERVAL_MINUTES", "ten"),
    ("MAX_DELETE_FRACTION", "1.5"),
    ("MAX_DELETE_FRACTION", "lots"),
    ("DRY_RUN", "maybe"),
    ("LOG_LEVEL", "verbose"),
])
def test_malformed_values_name_the_variable(var, value):
    assert any(var in p for p in problems(**{var: value}))


def test_bool_and_number_parsing():
    cfg = load(DRY_RUN="false", SYNC_INTERVAL_MINUTES="5", MAX_DELETE_FRACTION="0", LOG_LEVEL="DEBUG",
               LDAP_ATTR_NAME="cn, uid", LDAP_ATTR_MAIL="userPrincipalName")
    assert (cfg.dry_run, cfg.sync_interval_minutes, cfg.max_delete_fraction, cfg.log_level) == (False, 5, 0.0, "debug")
    assert cfg.ldap_attr_name == ("cn", "uid")
    assert cfg.ldap_attr_mail == "userPrincipalName"


def test_secrets_not_in_errors_or_repr():
    p = problems(DRY_RUN="maybe")
    assert not any("secret" in x for x in p)
    cfg = load()
    assert "secret" not in repr(cfg)
