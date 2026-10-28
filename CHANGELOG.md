# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## 0.0.2 - 2026-10-28

Pre-release for connectivity testing. No address book sync yet.

### Added

- `examples/compose.with-azuread-ldap-wrapper.yaml`: the sync next to
  AzureAD-LDAP-wrapper, with healthchecks for both services. The sync starts
  only once the wrapper is healthy.
- Healthchecks in `examples/compose.yaml`.
- The connectivity check reports whether `/data` is writable.
- `check --wait SECONDS` waits for the LDAP server to accept connections. The
  container's startup check waits up to 120 s.
- Full setup, operations and troubleshooting guide in the README.

### Fixed

- The startup check no longer fails when the LDAP server is still starting
  after a deploy.
- A failed LDAP bind now closes its connection instead of leaking it.
- The LDAP TLS check suggests `LDAP_TLS_NAME` only for a name mismatch, not
  for an untrusted or expired certificate.
- An IPv6 printer address without brackets is rejected with a clear message,
  instead of producing a broken URL.
- Refuse to log in when `TS_AUTHKEY` is a Headscale key (`hskey-`) and
  `TS_LOGIN_SERVER` is not set, instead of sending the key to Tailscale's
  default control server.

## 0.0.1 - 2026-10-28

Pre-release for connectivity testing. No address book sync yet.

### Added

- Container image with an embedded Tailscale client (userspace networking,
  outbound HTTP proxy for printer traffic only), started when `TS_AUTHKEY` is set.
- Environment validation that names every missing or malformed variable.
- LDAP reader with certificate validation against `LDAP_TLS_NAME`.
- `python -m ricoh_ldap_sync.check`: read-only connectivity check for the
  tailnet, printers and LDAP.

## 0.1.0

First release with address book sync. Not released yet.
