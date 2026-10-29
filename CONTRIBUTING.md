# Contributing

Bug reports, printer compatibility reports and pull requests are welcome.

## Before you open an issue

- Run `python -m ricoh_ldap_sync.check` and, for printer problems,
  `python -m ricoh_ldap_sync.probe` (add `--write-test` if writes fail).
  Include the output.
- Remove names, email addresses, passwords, auth keys and internal hostnames
  from anything you paste. The check and probe output is already scrubbed, but
  look before posting.
- Report security problems privately (see [SECURITY.md](SECURITY.md)).

## Pull requests

```bash
python3.15 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

- Tests must run with no network, printer or LDAP server. Use the simulated
  printer in `tests/fakeprinter.py`.
- Test data is synthetic: made-up names and `@example.com` addresses, and
  `192.0.2.x` for IPs. Never commit captures from a real device. Keep them in
  `recordings/`, which git ignores.
- Anything that changes what the sync writes, updates or deletes needs a test,
  and must keep the [safety rules](README.md#safety): only touch owned entries,
  never delete because a source came back empty or failed.
- Pin new dependencies exactly in `requirements.txt`, including transitive ones.
- Add a line to `CHANGELOG.md` under `Unreleased`.

## Releases

Maintainers tag `vX.Y.Z` on `main`. The build workflow runs the tests and
publishes `ghcr.io/meyeroppelt/ricoh-ldap-sync:X.Y.Z`.
