# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub security advisories](https://github.com/MeyerOppelt/ricoh-ldap-sync/security/advisories/new).
Do not open a public issue.

## What the container holds

A running container has:

- the admin password for each printer's address book,
- the LDAP bind password, and
- a Tailscale node key (in `/var/lib/tailscale`) when Tailscale is enabled.

Run it only on a host you trust, keep the volumes private to that host, and give
the LDAP bind account read-only access. The tool never logs these secrets.
