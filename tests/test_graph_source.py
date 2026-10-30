import pytest

from ricoh_ldap_sync import config, graph_source

GROUP = "11111111-2222-3333-4444-555555555555"
ENV = {
    "SOURCE": "graph",
    "GRAPH_TENANT_ID": "tenant.example.com",
    "GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "GRAPH_CLIENT_SECRET": "secret-graph",
    "GRAPH_GROUP_ID": GROUP,
    "PRINTERS": '[{"name":"office","host":"192.0.2.10"}]',
    "RICOH_PASSWORD": "pw",
}
MEMBERS_URL = (f"{graph_source.GRAPH_URL}/groups/{GROUP}/transitiveMembers/microsoft.graph.user"
               f"?$select={graph_source.SELECT}&$top=999")


@pytest.fixture
def cfg():
    return config.load(ENV)


class Response:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class Session:
    """Answers each URL with the next queued response."""

    def __init__(self, routes):
        self.routes = {url: list(responses) for url, responses in routes.items()}
        self.headers = {}
        self.posted = []

    def post(self, url, data=None, timeout=None):
        self.posted.append(data)
        return self.routes[url].pop(0)

    def get(self, url, timeout=None):
        return self.routes[url].pop(0)


def member(name="", **fields):
    return {"displayName": name, "userType": "Member", "accountEnabled": True, **fields}


def test_everyone_in_the_group_is_included(cfg):
    users = graph_source.to_users([
        member("Alice Anders", mail="alice@example.com"),
        member("Disabled Dana", mail="dana@example.com", accountEnabled=False),
        member("Pending Pat", mail="pat@partner.example", userType="Guest", externalUserState="PendingAcceptance"),
        member("Personal Pia", mail="pia@mail.example", userType="Guest", externalUserState="Accepted"),
        member("Never Invited", mail="nina@partner.example", userType="Guest", externalUserState=None),
    ], cfg)
    assert [u.email for u in users] == ["alice@example.com", "dana@example.com", "nina@partner.example",
                                        "pat@partner.example", "pia@mail.example"]


def test_address_fallbacks(cfg):
    users = graph_source.to_users([
        member("From Other Mails", otherMails=["Other@Partner.example"], userPrincipalName="x_partner.example#EXT#@t.example"),
        member("From Guest UPN", userPrincipalName="gus_partner.example#EXT#@tenant.onmicrosoft.com"),
        member("From Member UPN", userPrincipalName="mo@example.com"),
        member("Mail Is Guest UPN", mail="gina_partner.example#EXT#@tenant.onmicrosoft.com"),
        member("Placeholder Mail", mail="null", otherMails=["paula@partner.example"]),
    ], cfg)
    assert {u.name: u.email for u in users} == {
        "From Other Mails": "other@partner.example",
        "From Guest UPN": "gus@partner.example",
        "From Member UPN": "mo@example.com",
        "Mail Is Guest UPN": "gina@partner.example",
        "Placeholder Mail": "paula@partner.example",
    }


def test_skips_only_members_without_any_address(cfg):
    users = graph_source.to_users([
        member("No Address", userPrincipalName="noaddress#EXT#@tenant.onmicrosoft.com"),
        member("Has Address", mail="has@example.com"),
    ], cfg)
    assert [u.name for u in users] == ["Has Address"]


def test_name_fallbacks_and_duplicates(cfg):
    users = graph_source.to_users([
        member("", givenName="Gail", surname="Given", mail="gail@example.com"),
        member("", mail="nameless@example.com"),
        member("First Copy", mail="dup@example.com"),
        member("Second Copy", mail="DUP@example.com"),
    ], cfg)
    assert {u.email: u.name for u in users} == {
        "gail@example.com": "Gail Given",
        "nameless@example.com": "nameless@example.com",
        "dup@example.com": "First Copy",
    }


def test_company_tags_and_guest_tag(cfg):
    member_alice = member("Alice Anders", mail="alice@example.com", companyName="  Example   Holdings ")
    guest_tomas = member("Tomas Lindqvist", mail="tomas@partner.example", userType="Guest", companyName="NWT")
    users = {u.email: u for u in graph_source.to_users([member_alice, guest_tomas], cfg)}
    assert (users["alice@example.com"].company, users["alice@example.com"].tag) == ("Example Holdings", "Exam")
    assert users["tomas@partner.example"].tag == "NWT"

    tagged = config.load({**ENV, "GRAPH_TAG_GUESTS": "true", "EXTERNAL_TAG": "GST"})
    users = {u.email: u for u in graph_source.to_users([member_alice, guest_tomas], tagged)}
    assert users["tomas@partner.example"].tag == "GST"
    assert users["alice@example.com"].tag == "Exam"


def test_members_follows_every_page(cfg):
    page2 = f"{graph_source.GRAPH_URL}/next-page"
    s = Session({
        MEMBERS_URL: [Response(body={"value": [member("A", mail="a@example.com")], "@odata.nextLink": page2})],
        page2: [Response(body={"value": [member("B", mail="b@example.com")]})],
    })
    assert [m["displayName"] for m in graph_source.members(s, cfg)] == ["A", "B"]


def test_throttling_is_retried(cfg):
    waits = []
    s = Session({MEMBERS_URL: [Response(429, headers={"Retry-After": "7"}), Response(body={"value": []})]})
    assert graph_source.get(s, MEMBERS_URL, sleep=waits.append) == {"value": []}
    assert waits == [7]


def test_forbidden_names_the_permissions(cfg):
    s = Session({MEMBERS_URL: [Response(403, {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})]})
    with pytest.raises(graph_source.GraphError, match="GroupMember.Read.All and User.Read.All"):
        graph_source.members(s, cfg)


def test_missing_group_says_check_group_id(cfg):
    url = f"{graph_source.GRAPH_URL}/groups/{GROUP}?$select=id,displayName"
    s = Session({url: [Response(404, {"error": {"code": "Request_ResourceNotFound", "message": "not found"}})]})
    with pytest.raises(graph_source.GraphError, match="check GRAPH_GROUP_ID"):
        graph_source.group(s, cfg)


def test_sign_in_sets_token_and_never_reports_the_secret(cfg):
    url = f"{graph_source.LOGIN_URL}/tenant.example.com/oauth2/v2.0/token"
    s = Session({url: [Response(body={"access_token": "tok"})]})
    graph_source.sign_in(s, cfg)
    assert s.headers["Authorization"] == "Bearer tok"
    assert s.posted[0]["grant_type"] == "client_credentials"

    s = Session({url: [Response(401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret.\nTrace ID: x"})]})
    with pytest.raises(graph_source.GraphError) as e:
        graph_source.sign_in(s, cfg)
    assert "AADSTS7000215" in str(e.value)
    assert "Trace ID" not in str(e.value)
    assert "secret-graph" not in str(e.value)


def test_describe_counts_what_a_wrapper_would_leave_out():
    found = [
        member("A", mail="a@example.com"),
        member("B", mail="b@example.com", accountEnabled=False),
        member("C", mail="c@partner.example", userType="Guest", externalUserState="PendingAcceptance"),
        member("D", mail="d@partner.example", userType="Guest", externalUserState="Accepted"),
    ]
    assert graph_source.describe(found) == "4 users in the group (1 guests not yet accepted, 1 disabled; all included)"
