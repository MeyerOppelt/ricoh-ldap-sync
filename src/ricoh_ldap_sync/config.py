"""Environment parsing and validation.

Every setting comes from environment variables. ``load()`` collects every
problem it finds and raises a single ``ConfigError`` naming each bad variable,
so a misconfigured container fails once with the whole list. Secret values are
never included in error messages.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# tailscaled listens here when TS_AUTHKEY is set (see entrypoint.sh).
TAILSCALE_PROXY = "http://localhost:1055"

DEFAULT_LDAP_FILTER = "(&(uid=*)(mail=*))"
DEFAULT_ATTR_NAME = "displayName,cn,uid"
DEFAULT_ATTR_MAIL = "mail"
DEFAULT_RICOH_USER = "admin"
DEFAULT_TS_HOSTNAME = "ricoh-ldap-sync"
DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_MAX_DELETE_FRACTION = 0.3
LOG_LEVELS = ("debug", "info", "warning", "error")
SOURCES = ("ldap", "graph")

_PRINTER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Printer:
    name: str
    host: str
    password: str = field(repr=False)
    capacity: int | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"


@dataclass(frozen=True)
class Config:
    source: str = "ldap"
    ldap_url: str = ""
    ldap_tls_name: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = field(default="", repr=False)
    ldap_base: str = ""
    ldap_filter: str = DEFAULT_LDAP_FILTER
    ldap_attr_name: tuple[str, ...] = ("displayName", "cn", "uid")
    ldap_attr_mail: str = DEFAULT_ATTR_MAIL
    ldap_attr_company: str = ""
    ldap_attr_external: str = ""
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = field(default="", repr=False)
    graph_group_id: str = ""
    graph_tag_guests: bool = False
    external_tag: str = "EXT"
    company_tags: dict[str, str] = field(default_factory=dict)
    printers: tuple[Printer, ...] = ()
    ricoh_user: str = DEFAULT_RICOH_USER
    tailscale_enabled: bool = False
    ts_login_server: str | None = None
    ts_hostname: str = DEFAULT_TS_HOSTNAME
    sync_interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    dry_run: bool = True
    keep_sorted: bool = False
    max_delete_fraction: float = DEFAULT_MAX_DELETE_FRACTION
    log_level: str = "info"

    @property
    def printer_proxy(self) -> str | None:
        return TAILSCALE_PROXY if self.tailscale_enabled else None

    @property
    def ldap_host(self) -> str:
        return urlsplit(self.ldap_url).hostname or ""

    @property
    def ldap_port(self) -> int:
        parts = urlsplit(self.ldap_url)
        return parts.port or (636 if parts.scheme == "ldaps" else 389)

    @property
    def ldap_use_ssl(self) -> bool:
        return urlsplit(self.ldap_url).scheme == "ldaps"


def password_var(printer_name: str) -> str:
    """Name of the per-printer password override, e.g. bldg-1 -> RICOH_PASSWORD_BLDG_1."""
    return "RICOH_PASSWORD_" + printer_name.upper().replace("-", "_")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    problems: list[str] = []

    def get(name: str) -> str:
        return env.get(name, "").strip()

    def required(name: str, hint: str = "") -> str:
        value = get(name)
        if not value:
            problems.append(f"{name} is required{hint}")
        return value

    source = (get("SOURCE") or "ldap").lower()
    if source not in SOURCES:
        problems.append(f"SOURCE must be one of {', '.join(SOURCES)} (got {source!r})")

    ldap_url = ldap_tls_name = ldap_bind_dn = ldap_bind_password = ldap_base = ""
    ldap_filter = get("LDAP_FILTER") or DEFAULT_LDAP_FILTER
    if source == "ldap":
        ldap_url = required("LDAP_URL", " (e.g. ldaps://ldap.example.com:636)")
        if ldap_url:
            try:
                parts = urlsplit(ldap_url)
                parts.port  # raises ValueError on a malformed port
            except ValueError:
                parts = None
            if parts is None or parts.scheme not in ("ldap", "ldaps") or not parts.hostname:
                problems.append("LDAP_URL must look like ldaps://host[:port] or ldap://host[:port]")
                ldap_url = ""
        ldap_tls_name = get("LDAP_TLS_NAME") or (urlsplit(ldap_url).hostname if ldap_url else None) or ""

        ldap_bind_dn = required("LDAP_BIND_DN")
        ldap_bind_password = required("LDAP_BIND_PASSWORD")
        ldap_base = required("LDAP_BASE")
        if not (ldap_filter.startswith("(") and ldap_filter.endswith(")")):
            problems.append("LDAP_FILTER must be an LDAP filter in parentheses, e.g. (&(uid=*)(mail=*))")

    graph_tenant_id = graph_client_id = graph_client_secret = graph_group_id = ""
    if source == "graph":
        graph_tenant_id = required("GRAPH_TENANT_ID", " (the Directory (tenant) ID of the app registration)")
        graph_client_id = required("GRAPH_CLIENT_ID", " (the Application (client) ID of the app registration)")
        graph_client_secret = required("GRAPH_CLIENT_SECRET")
        graph_group_id = required("GRAPH_GROUP_ID", " (the Object ID of the Entra group to sync)")
        if graph_group_id and not _GUID.match(graph_group_id):
            problems.append("GRAPH_GROUP_ID must be the group's Object ID, e.g. 00000000-0000-0000-0000-000000000000")
    graph_tag_guests = _bool(get("GRAPH_TAG_GUESTS"), False, "GRAPH_TAG_GUESTS", problems)

    attr_name = tuple(a.strip() for a in (get("LDAP_ATTR_NAME") or DEFAULT_ATTR_NAME).split(",") if a.strip())
    if not attr_name:
        problems.append("LDAP_ATTR_NAME must list at least one attribute")
    attr_mail = get("LDAP_ATTR_MAIL") or DEFAULT_ATTR_MAIL
    attr_company = get("LDAP_ATTR_COMPANY")
    attr_external = get("LDAP_ATTR_EXTERNAL")
    external_tag = get("EXTERNAL_TAG") or "EXT"
    if len(external_tag) > COMPANY_TAG_MAX:
        problems.append(f"EXTERNAL_TAG must be 1 to {COMPANY_TAG_MAX} characters")
    company_tags = _company_tags(get("COMPANY_TAGS"), problems)

    printers = _printers(env, required("PRINTERS", ' (JSON, e.g. [{"name":"office","host":"192.0.2.10"}])'), problems)

    ricoh_user = get("RICOH_USER") or DEFAULT_RICOH_USER

    interval = _number(get("SYNC_INTERVAL_MINUTES"), DEFAULT_INTERVAL_MINUTES, int, "SYNC_INTERVAL_MINUTES", problems)
    if interval is not None and interval < 1:
        problems.append("SYNC_INTERVAL_MINUTES must be at least 1")

    fraction = _number(get("MAX_DELETE_FRACTION"), DEFAULT_MAX_DELETE_FRACTION, float, "MAX_DELETE_FRACTION", problems)
    if fraction is not None and not 0 <= fraction <= 1:
        problems.append("MAX_DELETE_FRACTION must be between 0 and 1")

    dry_run = _bool(get("DRY_RUN"), True, "DRY_RUN", problems)
    keep_sorted = _bool(get("KEEP_SORTED"), False, "KEEP_SORTED", problems)

    log_level = (get("LOG_LEVEL") or "info").lower()
    if log_level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")

    if get("TS_AUTHKEY").startswith("hskey-") and not get("TS_LOGIN_SERVER"):
        problems.append("TS_AUTHKEY is a Headscale key, so TS_LOGIN_SERVER must be set to your Headscale URL")

    if problems:
        raise ConfigError(problems)

    return Config(
        source=source,
        ldap_url=ldap_url,
        ldap_tls_name=ldap_tls_name,
        ldap_bind_dn=ldap_bind_dn,
        ldap_bind_password=ldap_bind_password,
        ldap_base=ldap_base,
        ldap_filter=ldap_filter,
        ldap_attr_name=attr_name,
        ldap_attr_mail=attr_mail,
        ldap_attr_company=attr_company,
        ldap_attr_external=attr_external,
        graph_tenant_id=graph_tenant_id,
        graph_client_id=graph_client_id,
        graph_client_secret=graph_client_secret,
        graph_group_id=graph_group_id,
        graph_tag_guests=graph_tag_guests,
        external_tag=external_tag,
        company_tags=company_tags,
        printers=printers,
        ricoh_user=ricoh_user,
        tailscale_enabled=bool(get("TS_AUTHKEY")),
        ts_login_server=get("TS_LOGIN_SERVER") or None,
        ts_hostname=get("TS_HOSTNAME") or DEFAULT_TS_HOSTNAME,
        sync_interval_minutes=interval,
        dry_run=dry_run,
        keep_sorted=keep_sorted,
        max_delete_fraction=fraction,
        log_level=log_level,
    )


def _printers(env: Mapping[str, str], raw: str, problems: list[str]) -> tuple[Printer, ...]:
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        problems.append(f"PRINTERS is not valid JSON ({e.msg} at position {e.pos})")
        return ()
    if not isinstance(items, list) or not items:
        problems.append('PRINTERS must be a non-empty JSON list of {"name","host"} objects')
        return ()

    default_password = env.get("RICOH_PASSWORD", "").strip()
    printers: list[Printer] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"PRINTERS[{i}] must be an object with name and host")
            continue
        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        if not _PRINTER_NAME.match(name):
            problems.append(f"PRINTERS[{i}].name must be letters, digits, '-' or '_' (got {name!r})")
            continue
        if not host or "/" in host or " " in host:
            problems.append(f"PRINTERS[{i}].host must be a hostname or IP, optionally with :port, no scheme or path")
            continue
        if host.count(":") > 1 and not host.startswith("["):
            problems.append(f"PRINTERS[{i}].host: put an IPv6 address in brackets, e.g. [2001:db8::10]")
            continue
        capacity = item.get("capacity")
        if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1):
            problems.append(f"PRINTERS[{i}].capacity must be a positive whole number")
            continue
        var = password_var(name)
        if var in seen:
            problems.append(f"PRINTERS has more than one printer named {name!r} (names must be unique ignoring case and -/_)")
            continue
        seen.add(var)
        password = env.get(var, "").strip() or default_password
        if not password:
            problems.append(f"RICOH_PASSWORD (or {var}) is required for printer {name!r}")
            continue
        printers.append(Printer(name=name, host=host, password=password, capacity=capacity))
    return tuple(printers)


COMPANY_TAG_MAX = 6


def _company_tags(raw: str, problems: list[str]) -> dict[str, str]:
    if not raw:
        return {}
    try:
        tags = json.loads(raw)
    except json.JSONDecodeError as e:
        problems.append(f"COMPANY_TAGS is not valid JSON ({e.msg} at position {e.pos})")
        return {}
    if not isinstance(tags, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
        problems.append('COMPANY_TAGS must be a JSON object of company name to tag, e.g. {"Example Holdings":"EH"}')
        return {}
    bad = [k for k, v in tags.items() if not 1 <= len(v.strip()) <= COMPANY_TAG_MAX]
    if bad:
        problems.append(f"COMPANY_TAGS: each tag must be 1 to {COMPANY_TAG_MAX} characters (check {', '.join(bad)})")
        return {}
    return {" ".join(k.split()): v.strip() for k, v in tags.items()}


def _number(raw, default, kind, name, problems):
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        problems.append(f"{name} must be a{'n integer' if kind is int else ' number'} (got {raw!r})")
        return None


def _bool(raw: str, default: bool, name: str, problems: list[str]) -> bool:
    if not raw:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    problems.append(f"{name} must be true or false (got {raw!r})")
    return default
