import ipaddress

import pytest

from ricoh_ldap_sync import tailnet

STATUS = {
    "BackendState": "Running",
    "Peer": {
        "nodekey:a": {"HostName": "router-a", "Online": True, "PrimaryRoutes": ["192.0.2.0/24"]},
        "nodekey:b": {"HostName": "router-b", "Online": False, "PrimaryRoutes": ["192.0.2.10/32"]},
        "nodekey:c": {"HostName": "laptop", "Online": True},
    },
}


@pytest.mark.parametrize("host, expected", [
    ("192.0.2.10", "192.0.2.10"),
    ("192.0.2.10:8080", "192.0.2.10"),
    ("[2001:db8::1]:80", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
    ("printer.example.com", None),
    ("printer.example.com:80", None),
])
def test_host_ip(host, expected):
    ip = tailnet.host_ip(host)
    assert (str(ip) if ip else None) == expected


def test_most_specific_route_wins():
    route = tailnet.route_for(ipaddress.ip_address("192.0.2.10"), STATUS)
    assert route == tailnet.Route(peer="router-b", prefix="192.0.2.10/32", online=False)


def test_broader_route():
    route = tailnet.route_for(ipaddress.ip_address("192.0.2.20"), STATUS)
    assert route.peer == "router-a" and route.online


def test_no_route():
    assert tailnet.route_for(ipaddress.ip_address("198.51.100.5"), STATUS) is None
    assert tailnet.route_for(ipaddress.ip_address("198.51.100.5"), {"Peer": None}) is None
