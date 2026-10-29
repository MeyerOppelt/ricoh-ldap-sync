# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## 0.6.0 - 2026-10-29

### Changed

- Entries are ordered by company, then by person. Each company's people sit
  together, companies in alphabetical order of their full name, people
  alphabetical within it. People without a company come first. This applies to
  each batch of adds and to `KEEP_SORTED` renumbering. Without
  `LDAP_ATTR_COMPANY` the order is unchanged (by name). With `KEEP_SORTED`, the
  first run after upgrading renumbers once into the new order.

## 0.5.1 - 2026-10-29

### Changed

- The tag now comes before the name on the button label: `(EH) Alice A.`
  instead of `Alice A. (EH)`. A company's people then sit together when the
  panel sorts by display name. Tagged entries are relabelled on the next run.
  Names don't change, so nobody is renumbered.

## 0.5.0 - 2026-10-29

### Added

- `LDAP_ATTR_EXTERNAL` tags external users' button labels with `EXTERNAL_TAG`
  (default `EXT`), for example `Brad W. (EXT)`. The Name field keeps their
  full display name. With AzureAD-LDAP-wrapper, use `AzureADuserExternal`,
  which the wrapper already provides, so no customizer is needed. A user whose
  mail was a guest UPN also counts as external. If company tags are also on,
  external users get the external tag and members get their company tag.

## 0.4.2 - 2026-10-29

### Changed

- A company without a `COMPANY_TAGS` entry is tagged with the first 4
  characters of its name (for example `Exam` for "Example Holdings") instead of
  its initials. `COMPANY_TAGS` is only needed for overrides. If you relied on
  initials, the affected labels are updated on the next run.

## 0.4.1 - 2026-10-29

### Fixed

- A guest's user principal name (`user_partner.example#EXT#@tenant...`) found
  in `mail` is turned back into their real address (`user@partner.example`)
  instead of being used as a destination. If the address can't be recovered,
  the person is skipped.
- `examples/azuread-ldap-wrapper/ldap_customizer.js` uses a guest's
  `otherMails` address when their Entra `mail` is empty.

## 0.4.0 - 2026-10-29

### Added

- Company tag on the panel's button label. Set `LDAP_ATTR_COMPANY` to the
  LDAP attribute holding each person's company, and optionally `COMPANY_TAGS`
  for short tags (unlisted companies use their initials). The label becomes
  `Alice A. (EH)`, and the Name field keeps the full name. A company change
  updates the entry.
- `examples/azuread-ldap-wrapper/ldap_customizer.js`: makes
  AzureAD-LDAP-wrapper serve Entra's Company name as the `company` attribute.
- The state file records the label written, so a label change is detected.
  Records from earlier versions are read as having the plain name, so
  upgrading without a company attribute changes nothing.

### Fixed

- Directory entries whose `mail` isn't an email address (for example the
  literal `null` for an account with no mailbox) are skipped instead of added.
- A run where the safety brake held back deletes now counts as failed, so the
  healthcheck reports it. When LDAP returns 0 users, the log shows one line
  with the base and filter used, instead of a line for every entry.

## 0.3.0 - 2026-10-29

### Changed

- `KEEP_SORTED` now deletes the sync's entries before re-adding them in name
  order. In 0.2.0, copies were added first, so every renumber pushed the
  registration numbers up by the size of the list. Now they start again from
  the lowest free number. Before deleting, each entry is recorded as awaiting
  re-add, so a failed or interrupted renumber is finished by the next run. Until
  then, the affected people are off the panel. If the delete fails, nothing is
  re-added.
- A renumber no longer needs room for a temporary copy of every entry.
- Leftover copies from an interrupted 0.2.0 renumber are still cleaned up.
- `tailscaled`'s `[RATELIMIT]` log notices are filtered out. They only meant
  it was holding back repeats of a debug message.

### Added

- `python -m ricoh_ldap_sync --once --renumber` re-adds the sync's entries in
  name order from the lowest free number, even if they're already sorted.
  Use it to bring down numbers left high by 0.2.0.

## 0.2.0 - 2026-10-29

### Added

- `KEEP_SORTED=true` keeps the sync's entries in alphabetical order. When
  their registration numbers are out of name order (a new person who sorts
  earlier, or a rename), the sync adds a fresh copy of each of its entries in
  name order, then deletes the old ones. Adding first means nobody disappears
  if something fails. A failed renumber is rolled back, and an interrupted one
  is finished or undone on the next run. Hand-made entries are never touched.
  Off by default.

### Changed

- New entries are added in name order (ignoring case and accents) instead of
  email order, so each batch is alphabetical on the panel.

## 0.1.0 - 2026-10-29

First release.

### Added

- `python -m ricoh_ldap_sync --once` runs one sync now and exits with 0 on
  success. `--dry-run` previews a run without writing, whatever `DRY_RUN` says.
  `--version` prints the version.
- A lock in `/data` so a manual run and the scheduled worker never overlap.
- The version is logged at startup.
- `docs/protocol.md`: reference for the address book SOAP calls and properties.
- `CONTRIBUTING.md` and issue templates for bugs and printer compatibility
  reports.

### Fixed

- An unconfirmed add whose email now belongs to a different entry is dropped
  from the state file, instead of being kept indefinitely.

## 0.0.3 - 2026-10-29

Pre-release. The full sync, tested against a simulated printer. Confirm writes
on your printer with the probe before setting `DRY_RUN=false`.

### Added

- Address book sync: adds users missing from the printer, updates entries whose
  display name changed, and deletes entries for users no longer in LDAP. It
  only touches entries it created, which it records in `/data/state.json`.
- Safety brake: no deletes when LDAP returns 0 users, or when deletes would
  remove more than `MAX_DELETE_FRACTION` of a printer's managed entries.
- Adoption of existing entries that exactly match, when there is no state.
  Hand-made entries with a user's email are left alone, with a warning.
- Dry run (the default) logs the plan and writes nothing, not even state.
- Optional per-printer `capacity` in `PRINTERS`. Adds stop when it's reached.
- `python -m ricoh_ldap_sync.probe [--write-test]`: proves the address book
  service works on a printer, with names and emails scrubbed from the output.
- The container now runs the worker. It runs the connectivity check at start,
  then syncs every `SYNC_INTERVAL_MINUTES`.

### Changed

- The healthcheck now also fails until the first successful run.
- Base image moved to Python 3.15 (`python:3.15.0-slim-trixie`). Tests run on
  3.15 in CI to match.

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
