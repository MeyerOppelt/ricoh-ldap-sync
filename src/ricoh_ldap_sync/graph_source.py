"""Read the members of one Microsoft Entra group from Microsoft Graph.

Every user in the group is included, nested groups too: guests who haven't
accepted their invitation, guests with personal Microsoft or Google accounts,
and disabled accounts. An address book entry needs a name and an address, never
a login, so none of them is a reason to leave someone out.

Any failure raises ``GraphError``. Callers must treat that as "skip this run",
never as "the group is empty".
"""

from __future__ import annotations

import logging
import time

import requests

from .config import Config
from .ldap_source import _EMAIL, User, company_tag, guest_address

log = logging.getLogger(__name__)

LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
TIMEOUT = (10, 30)
MAX_RETRIES = 3
MAX_RETRY_WAIT = 60
SELECT = ("id,displayName,givenName,surname,mail,otherMails,userPrincipalName,"
          "userType,companyName,accountEnabled,externalUserState")
PERMISSION_HINT = ("the app registration needs the Microsoft Graph application permissions "
                   "GroupMember.Read.All and User.Read.All, with admin consent")


class GraphError(Exception):
    pass


def _detail(resp: requests.Response) -> str:
    """Microsoft's own error code and first line of its message, never the request."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error")
    if isinstance(err, dict):  # Graph
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    if isinstance(err, str):  # sign-in endpoint
        description = (body.get("error_description") or "").splitlines()
        return f"{err}: {description[0] if description else ''}".strip(": ")
    return ""


def sign_in(session: requests.Session, cfg: Config) -> None:
    """Get an app-only token and attach it to the session."""
    try:
        resp = session.post(
            f"{LOGIN_URL}/{cfg.graph_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": cfg.graph_client_id,
                "client_secret": cfg.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise GraphError(f"cannot reach {LOGIN_URL}: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise GraphError(f"sign-in as app {cfg.graph_client_id} failed: HTTP {resp.status_code} {_detail(resp)}".strip())
    session.headers["Authorization"] = "Bearer " + resp.json()["access_token"]


def get(session: requests.Session, url: str, *, sleep=time.sleep) -> dict:
    """GET a Graph URL, waiting and retrying when Graph throttles or is briefly unavailable."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise GraphError(f"cannot reach {GRAPH_URL}: {type(e).__name__}") from e
        if resp.status_code in (429, 503, 504) and attempt < MAX_RETRIES:
            try:
                wait = min(int(resp.headers.get("Retry-After", "")), MAX_RETRY_WAIT)
            except ValueError:
                wait = 2 ** (attempt + 1)
            log.info("graph: HTTP %d, retrying in %ds", resp.status_code, wait)
            sleep(wait)
            continue
        if resp.status_code == 200:
            return resp.json()
        hint = f" ({PERMISSION_HINT})" if resp.status_code == 403 else ""
        raise GraphError(f"HTTP {resp.status_code} {_detail(resp)}{hint}".strip())
    raise AssertionError("unreachable")


def group(session: requests.Session, cfg: Config) -> dict:
    """The group itself, so a wrong GRAPH_GROUP_ID fails with a clear message."""
    try:
        return get(session, f"{GRAPH_URL}/groups/{cfg.graph_group_id}?$select=id,displayName")
    except GraphError as e:
        if str(e).startswith("HTTP 404"):
            raise GraphError(f"group {cfg.graph_group_id} not found; check GRAPH_GROUP_ID") from e
        raise


def members(session: requests.Session, cfg: Config) -> list[dict]:
    """Every user in the group, including members of nested groups, following every page."""
    url = f"{GRAPH_URL}/groups/{cfg.graph_group_id}/transitiveMembers/microsoft.graph.user?$select={SELECT}&$top=999"
    found: list[dict] = []
    while url:
        page = get(session, url)
        found.extend(page.get("value") or [])
        url = page.get("@odata.nextLink")
    return found


def address(member: dict) -> str:
    """The best address for scans: mail, then otherMails, then the user principal name.

    A guest's user principal name is turned back into their real address
    (see ``guest_address``). Returns "" when nothing usable is left.
    """
    for value in [member.get("mail"), *(member.get("otherMails") or []), member.get("userPrincipalName")]:
        if not isinstance(value, str):
            continue
        value = value.strip().lower()
        if "#ext#" in value:
            value = guest_address(value)
        if value and _EMAIL.match(value):
            return value
    return ""


def to_users(found: list[dict], cfg: Config) -> list[User]:
    """One user per member with an address, skipping duplicate addresses."""
    by_email: dict[str, User] = {}
    for m in found:
        given_surname = " ".join(p.strip() for p in (m.get("givenName"), m.get("surname")) if p and p.strip())
        label = (m.get("displayName") or "").strip() or given_surname
        email = address(m)
        if not email:
            log.warning("graph: skipping %s: no mail, otherMails or user principal name is an email address",
                        label or m.get("id") or "member")
            continue
        if email in by_email:
            log.warning("graph: duplicate email %s, keeping the first member", email)
            continue
        company = " ".join((m.get("companyName") or "").split())
        tag = ""
        if cfg.graph_tag_guests and m.get("userType") == "Guest":
            tag = cfg.external_tag
        elif company:
            tag = company_tag(company, cfg.company_tags)
        by_email[email] = User(email=email, name=label or email, tag=tag, company=company)
    return sorted(by_email.values(), key=lambda u: u.email)


def describe(found: list[dict]) -> str:
    """Counts of the members an LDAP wrapper would typically leave out, for the log."""
    pending = sum(1 for m in found if m.get("userType") == "Guest" and m.get("externalUserState") != "Accepted")
    disabled = sum(1 for m in found if m.get("accountEnabled") is False)
    return f"{len(found)} users in the group ({pending} guests not yet accepted, {disabled} disabled; all included)"


def fetch_users(cfg: Config) -> list[User]:
    with requests.Session() as session:
        sign_in(session, cfg)
        group(session, cfg)
        found = members(session, cfg)
    log.info("graph: %s", describe(found))
    return to_users(found, cfg)
