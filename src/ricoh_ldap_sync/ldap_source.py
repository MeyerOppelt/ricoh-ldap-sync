"""Read users from the LDAP directory.

Any failure raises ``LdapError``. Callers must treat that as "skip this run",
never as "the directory is empty".
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass

from ldap3 import Connection, Server, Tls
from ldap3.core.exceptions import LDAPException

from .config import Config

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
RECEIVE_TIMEOUT = 30


class LdapError(Exception):
    pass


@dataclass(frozen=True)
class User:
    email: str  # lowercased
    name: str


def make_server(cfg: Config) -> Server:
    tls = None
    if cfg.ldap_use_ssl:
        # Connect to cfg.ldap_host (which may be a container name) but require the
        # certificate to be valid for cfg.ldap_tls_name. ldap3 accepts a match on
        # either name; a public CA will not issue for an internal container name.
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            sni=cfg.ldap_tls_name,
            valid_names=[cfg.ldap_tls_name],
        )
    return Server(
        cfg.ldap_host,
        port=cfg.ldap_port,
        use_ssl=cfg.ldap_use_ssl,
        tls=tls,
        connect_timeout=CONNECT_TIMEOUT,
        get_info=None,
    )


def connect(cfg: Config) -> Connection:
    conn = Connection(
        make_server(cfg),
        user=cfg.ldap_bind_dn,
        password=cfg.ldap_bind_password,
        receive_timeout=RECEIVE_TIMEOUT,
        raise_exceptions=False,
    )
    try:
        if not conn.bind():
            raise LdapError(f"bind as {cfg.ldap_bind_dn} failed: {conn.result.get('description')} {conn.result.get('message') or ''}".strip())
    except LDAPException as e:
        raise LdapError(f"cannot connect to {cfg.ldap_url}: {e}") from e
    return conn


def search(conn: Connection, cfg: Config) -> list[dict[str, list[str]]]:
    """Run the configured search and return each entry's attributes as lists of strings."""
    attributes = sorted({*cfg.ldap_attr_name, cfg.ldap_attr_mail})
    try:
        ok = conn.search(cfg.ldap_base, cfg.ldap_filter, attributes=attributes)
    except LDAPException as e:
        raise LdapError(f"search failed: {e}") from e
    # A size or time limit returns a partial result set, so anything other than
    # result code 0 is an error. ldap3 returns False for a successful empty search.
    if not ok and conn.result.get("result") != 0:
        raise LdapError(f"search failed: {conn.result.get('description')} {conn.result.get('message') or ''}".strip())
    entries = []
    for entry in conn.response or []:
        if entry.get("type") != "searchResEntry":
            continue
        attrs = {}
        for key, value in (entry.get("attributes") or {}).items():
            values = value if isinstance(value, list) else [value]
            attrs[key] = [str(v) for v in values if v not in (None, "", b"")]
        entries.append(attrs)
    return entries


def to_users(entries: list[dict[str, list[str]]], cfg: Config) -> list[User]:
    """Pick a name and email for each entry, skipping incomplete entries and duplicate emails."""
    by_email: dict[str, User] = {}
    for attrs in entries:
        lower = {k.lower(): v for k, v in attrs.items()}
        mails = lower.get(cfg.ldap_attr_mail.lower()) or []
        email = mails[0].strip().lower() if mails else ""
        name = next((v[0].strip() for a in cfg.ldap_attr_name if (v := lower.get(a.lower())) and v[0].strip()), "")
        if not email or not name:
            log.warning("ldap: skipping entry without %s", "mail" if not email else "a name attribute")
            continue
        if email in by_email:
            log.warning("ldap: duplicate email %s, keeping the first entry", email)
            continue
        by_email[email] = User(email=email, name=name)
    return sorted(by_email.values(), key=lambda u: u.email)


def fetch_users(cfg: Config) -> list[User]:
    conn = connect(cfg)
    try:
        return to_users(search(conn, cfg), cfg)
    finally:
        conn.unbind()
