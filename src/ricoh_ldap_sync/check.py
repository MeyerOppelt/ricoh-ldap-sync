"""Connectivity check: tailnet, printer reachability and LDAP or Graph, without changing anything.

Run inside the container with ``python -m ricoh_ldap_sync.check``. Prints one
PASS/FAIL/INFO line per check and exits non-zero if any check failed. It prints
counts only, never names, email addresses or secrets.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import ssl
import sys
import time

import requests

from . import config, graph_source, ldap_source, tailnet

PRINTER_TIMEOUT = (10, 20)
DATA_DIR = "/data"


class Report:
    def __init__(self) -> None:
        self.failed = False

    def line(self, status: str, check: str, detail: str) -> None:
        if status == "FAIL":
            self.failed = True
        print(f"{status:<4} {check}: {detail}", flush=True)


def check_tailnet(cfg: config.Config, r: Report) -> None:
    if not cfg.tailscale_enabled:
        r.line("INFO", "tailscale", "TS_AUTHKEY not set; printers are reached directly")
        return
    try:
        st = tailnet.status()
    except tailnet.TailnetError as e:
        r.line("FAIL", "tailscale", str(e))
        return
    state = st.get("BackendState")
    me = st.get("Self") or {}
    r.line("PASS" if state == "Running" else "FAIL", "tailscale",
           f"state {state}, node {me.get('HostName')} {', '.join(me.get('TailscaleIPs') or [])}")
    try:
        accept = tailnet.prefs().get("RouteAll")
        r.line("PASS" if accept else "FAIL", "tailscale accept-routes", str(bool(accept)))
    except tailnet.TailnetError as e:
        r.line("FAIL", "tailscale accept-routes", str(e))
    for p in cfg.printers:
        ip = tailnet.host_ip(p.host)
        if ip is None:
            r.line("INFO", f"route {p.name}", f"{p.host} is a hostname; skipping the subnet route check")
            continue
        route = tailnet.route_for(ip, st)
        if route is None:
            r.line("FAIL", f"route {p.name}",
                   f"no peer has an approved route covering {ip}. Check the subnet router advertises it "
                   "and the route is approved in the control server")
        else:
            r.line("PASS" if route.online else "FAIL", f"route {p.name}",
                   f"{ip} via {route.peer} ({route.prefix}){'' if route.online else ', but that peer is offline'}")


def check_printers(cfg: config.Config, r: Report) -> None:
    proxies = {"http": cfg.printer_proxy, "https": cfg.printer_proxy} if cfg.printer_proxy else None
    with requests.Session() as s:
        s.trust_env = False  # never pick up a global HTTP_PROXY
        for p in cfg.printers:
            url = p.base_url + "/"
            try:
                resp = s.get(url, proxies=proxies, timeout=PRINTER_TIMEOUT)
            except requests.RequestException as e:
                hint = " (through the Tailscale proxy: is the subnet route approved and --accept-routes on?)" if proxies else ""
                r.line("FAIL", f"printer {p.name}", f"GET {url} failed: {type(e).__name__}{hint}")
                continue
            title = re.search(r"<title>(.*?)</title>", resp.text, re.I | re.S)
            looks_ricoh = "web image monitor" in resp.text.lower() or "/web/guest/" in resp.url
            r.line("PASS" if resp.status_code < 500 and looks_ricoh else "FAIL", f"printer {p.name}",
                   f"HTTP {resp.status_code}, final path {requests.utils.urlparse(resp.url).path}, "
                   f"title {title.group(1).strip()[:60]!r}" if title else f"HTTP {resp.status_code}, no title")


def wait_for_port(host: str, port: int, timeout: float, *, connect=socket.create_connection,
                  sleep=time.sleep, clock=time.monotonic) -> float | None:
    """Retry a TCP connect until it succeeds or timeout passes. Returns seconds waited, or None."""
    start = clock()
    while True:
        try:
            connect((host, port), timeout=5).close()
            return clock() - start
        except OSError:
            if clock() - start >= timeout:
                return None
            sleep(2)


def check_data(r: Report) -> None:
    probe = os.path.join(DATA_DIR, ".write-test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        r.line("PASS", "data volume", f"{DATA_DIR} is writable")
    except OSError as e:
        r.line("FAIL", "data volume", f"{DATA_DIR} is not writable: {e.strerror}")


def check_ldap(cfg: config.Config, r: Report, wait: float = 0) -> None:
    if wait:
        # Right after a deploy the LDAP server may still be starting.
        waited = wait_for_port(cfg.ldap_host, cfg.ldap_port, wait)
        if waited is None:
            r.line("FAIL", "ldap reachable", f"{cfg.ldap_host}:{cfg.ldap_port} not accepting connections after {wait:.0f}s")
        elif waited >= 1:
            r.line("INFO", "ldap reachable", f"{cfg.ldap_host}:{cfg.ldap_port} accepted connections after {waited:.0f}s")
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(cfg.ldap_host, cfg.ldap_port, type=socket.SOCK_STREAM)})
        r.line("PASS", "ldap dns", f"{cfg.ldap_host} -> {', '.join(addrs)}")
    except OSError as e:
        r.line("FAIL", "ldap dns", f"{cfg.ldap_host}: {e}")
        return

    if cfg.ldap_use_ssl:
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((cfg.ldap_host, cfg.ldap_port), timeout=10) as raw:
                with ctx.wrap_socket(raw, server_hostname=cfg.ldap_tls_name) as tls:
                    cert = tls.getpeercert()
            issuer = dict(x[0] for x in cert.get("issuer", ()))
            r.line("PASS", "ldap tls", f"certificate valid for {cfg.ldap_tls_name}, issuer {issuer.get('organizationName') or issuer.get('commonName')}, "
                   f"expires {cert.get('notAfter')}")
        except ssl.SSLCertVerificationError as e:
            if e.verify_code == 62:  # X509_V_ERR_HOSTNAME_MISMATCH
                hint = "Set LDAP_TLS_NAME to the name on the certificate"
            else:
                hint = "The certificate is not trusted by the container's CA store (self-signed, private CA or expired)"
            r.line("FAIL", "ldap tls", f"certificate not valid for {cfg.ldap_tls_name}: {e.verify_message}. {hint}")
            return
        except OSError as e:
            r.line("FAIL", "ldap tls", f"{cfg.ldap_host}:{cfg.ldap_port}: {e}")
            return
    else:
        r.line("INFO", "ldap tls", "LDAP_URL is ldap://, the bind password is sent unencrypted")

    try:
        conn = ldap_source.connect(cfg)
    except ldap_source.LdapError as e:
        r.line("FAIL", "ldap bind", str(e))
        return
    try:
        r.line("PASS", "ldap bind", f"bound as {cfg.ldap_bind_dn}")
        try:
            entries = ldap_source.search(conn, cfg)
        except ldap_source.LdapError as e:
            r.line("FAIL", "ldap search", str(e))
            return
        users = ldap_source.to_users(entries, cfg)
        have = {a: sum(1 for e in entries if any(k.lower() == a.lower() and v for k, v in e.items()))
                for a in (*cfg.ldap_attr_name, cfg.ldap_attr_mail)}
        r.line("PASS" if users else "FAIL", "ldap search",
               f"{len(entries)} entries, {len(users)} usable users; entries with each attribute: "
               + ", ".join(f"{a}={n}" for a, n in have.items()))
    finally:
        conn.unbind()


def check_graph(cfg: config.Config, r: Report) -> None:
    with requests.Session() as s:
        try:
            graph_source.sign_in(s, cfg)
        except graph_source.GraphError as e:
            r.line("FAIL", "graph sign-in", str(e))
            return
        r.line("PASS", "graph sign-in", f"app {cfg.graph_client_id} in tenant {cfg.graph_tenant_id}")
        try:
            g = graph_source.group(s, cfg)
            r.line("PASS", "graph group", f"{g.get('displayName')!r} ({cfg.graph_group_id})")
            found = graph_source.members(s, cfg)
        except graph_source.GraphError as e:
            r.line("FAIL", "graph group", str(e))
            return
    users = graph_source.to_users(found, cfg)
    r.line("PASS" if users else "FAIL", "graph members",
           f"{graph_source.describe(found)}; {len(users)} usable users")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ricoh_ldap_sync.check", description=__doc__.splitlines()[0])
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                        help="wait up to SECONDS for the LDAP server to accept connections (used at container start)")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    r = Report()
    source = (f"Microsoft Graph group {cfg.graph_group_id}" if cfg.source == "graph"
              else f"LDAP {cfg.ldap_url} (certificate name {cfg.ldap_tls_name})")
    r.line("INFO", "config", f"{len(cfg.printers)} printer(s): " + ", ".join(f"{p.name}={p.host}" for p in cfg.printers)
           + f"; {source}")
    check_data(r)
    check_tailnet(cfg, r)
    check_printers(cfg, r)
    if cfg.source == "graph":
        check_graph(cfg, r)
    else:
        check_ldap(cfg, r, wait=args.wait)
    print("RESULT: " + ("FAILED" if r.failed else "all checks passed"), flush=True)
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
