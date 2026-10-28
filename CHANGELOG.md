# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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
