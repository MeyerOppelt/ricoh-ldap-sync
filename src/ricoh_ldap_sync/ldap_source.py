"""Read users from the LDAP directory.

Any failure raises ``LdapError``. Callers must treat that as "skip this run",
never as "the directory is empty".
"""

from __future__ import annotations

import logging
import re
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


# A plausible address shaped like user@example.com, with no spaces. Rejects
# placeholders such as the literal "null" some directories return for no mail.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class User:
    email: str  # lowercased
    name: str
    tag: str = ""  # short company tag for the panel's button label, "" for none


def company_tag(company: str, tags: dict[str, str]) -> str:
    """Short tag for a company: from COMPANY_TAGS (case-insensitive), else its initials."""
    company = " ".join(company.split())
    if not company:
        return ""
    for name, tag in tags.items():
        if name.casefold() == company.casefold():
            return tag
    return "".join(word[0] for word in re.findall(r"[^\W_]+", company)).upper()[:4]


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
        if conn.bind():
            return conn
        error = LdapError(f"bind as {cfg.ldap_bind_dn} failed: {conn.result.get('description')} {conn.result.get('message') or ''}".strip())
    except LDAPException as e:
        error = LdapError(f"cannot connect to {cfg.ldap_url}: {e}")
    try:
        conn.unbind()
    except LDAPException:
        pass
    raise error


def search(conn: Connection, cfg: Config) -> list[dict[str, list[str]]]:
    """Run the configured search and return each entry's attributes as lists of strings."""
    attributes = sorted({*cfg.ldap_attr_name, cfg.ldap_attr_mail} | ({cfg.ldap_attr_company} if cfg.ldap_attr_company else set()))
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
        if email and not _EMAIL.match(email):
            log.warning("ldap: skipping %s: mail %r is not an email address", name or "entry", email)
            continue
        if not email or not name:
            log.warning("ldap: skipping entry without %s", "mail" if not email else "a name attribute")
            continue
        if email in by_email:
            log.warning("ldap: duplicate email %s, keeping the first entry", email)
            continue
        tag = ""
        if cfg.ldap_attr_company:
            companies = lower.get(cfg.ldap_attr_company.lower()) or []
            tag = company_tag(companies[0] if companies else "", cfg.company_tags)
        by_email[email] = User(email=email, name=name, tag=tag)
    return sorted(by_email.values(), key=lambda u: u.email)


def fetch_users(cfg: Config) -> list[User]:
    conn = connect(cfg)
    try:
        return to_users(search(conn, cfg), cfg)
    finally:
        conn.unbind()
