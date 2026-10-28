# ricoh-ldap-sync

Keeps the local address book on Ricoh multifunction printers in sync with the
users in an LDAP directory, so scan-to-email destinations stay current without
anyone editing the printer by hand.

It runs as one small container. It can reach printers on other networks through
an embedded Tailscale client (or a self-hosted Headscale server), so it can run
in the cloud next to your directory and still reach a printer in an office.

> **Status: pre-release.** `0.0.x` images contain the connectivity check only
> (`python -m ricoh_ldap_sync.check`). Address book sync arrives in `0.1.0`.
> The sections below describe how it is designed to work.

## How it works

Every `SYNC_INTERVAL_MINUTES` the worker:

1. Reads users from LDAP (`LDAP_BASE`, `LDAP_FILTER`). The display name is the
   first attribute in `LDAP_ATTR_NAME` that has a value. Emails are compared
   lowercased. If the LDAP query fails, the whole run is skipped for every
   printer. An error is never treated as "zero users".
2. For each printer, opens an address book session over the printer's SOAP
   interface (the one Ricoh's management tools use, not the web UI), reads the
   address book, and works out the changes:
   - **Add** each user who has no entry: name, key display, Title1 index tag from
     the first letter, and the email as a scan-to-email destination.
   - **Update** entries whose user's display name changed.
   - **Delete** entries whose user is no longer in LDAP.
3. Closes the session, always, even after an error. A stuck session can lock the
   address book on the printer's panel.

Each printer syncs on its own, so one printer failing doesn't block the others.

### Ownership: it only touches what it created

The sync records every entry it creates in `/data/state.json` (printer, email,
entry ID, name, last sync time). It only ever updates or deletes entries listed
there.

- If an entry someone created by hand already has a user's email, the sync
  leaves it alone, doesn't add a duplicate, and logs one warning per run.
- If the state file is missing, nothing is deleted. An existing entry is adopted
  only when its name and email exactly match what the sync would write. Each
  adoption is logged.

### Safety brake

For a printer, all deletes are skipped and an error is logged if:

- LDAP returned 0 users, or
- the deletes would remove more than `MAX_DELETE_FRACTION` (default `0.3`) of
  that printer's managed entries.

Adds and updates still go ahead.

### Dry run

`DRY_RUN=true` is the default. The worker logs the plan (adds, updates and
deletes for each printer) and writes nothing to any printer. Check the plan in
the logs, then set `DRY_RUN=false`.

## Configuration

All configuration is through environment variables. The container checks them
at startup and exits with a message naming each missing or malformed variable.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `LDAP_URL` | yes | | e.g. `ldaps://ldap.example.com:636` |
| `LDAP_TLS_NAME` | no | host from `LDAP_URL` | Name the server certificate must be valid for. Lets you connect to a container name while validating the certificate's real name. |
| `LDAP_BIND_DN` | yes | | |
| `LDAP_BIND_PASSWORD` | yes | | |
| `LDAP_BASE` | yes | | e.g. `ou=people,dc=example,dc=com` |
| `LDAP_FILTER` | no | `(&(uid=*)(mail=*))` | |
| `LDAP_ATTR_NAME` | no | `displayName,cn,uid` | Tried in order. |
| `LDAP_ATTR_MAIL` | no | `mail` | |
| `PRINTERS` | yes | | JSON list, e.g. `[{"name":"office","host":"192.0.2.10"}]`. Names may use letters, digits, `-` and `_`. |
| `RICOH_USER` | no | `admin` | |
| `RICOH_PASSWORD` | yes | | Used for every printer unless `RICOH_PASSWORD_<NAME>` is set (name uppercased, `-` replaced with `_`), e.g. `RICOH_PASSWORD_OFFICE`. |
| `TS_AUTHKEY` | no | | If unset, Tailscale doesn't start and printers are reached directly. |
| `TS_LOGIN_SERVER` | no | Tailscale's default | Set this for Headscale, e.g. `https://headscale.example.com`. |
| `TS_HOSTNAME` | no | `ricoh-ldap-sync` | |
| `SYNC_INTERVAL_MINUTES` | no | `15` | |
| `DRY_RUN` | no | `true` | |
| `MAX_DELETE_FRACTION` | no | `0.3` | |
| `LOG_LEVEL` | no | `info` | `debug`, `info`, `warning` or `error` |

Volumes:

| Path | Holds |
| --- | --- |
| `/data` | `state.json` (which entries the sync owns) and `last_success` (healthcheck). |
| `/var/lib/tailscale` | The Tailscale node identity, so the auth key is only used once. |

The container starts as root only to make these volumes writable by UID 1000,
then runs `tailscaled` and the worker as UID 1000.

## Deploying

### Docker Compose

See [`examples/compose.yaml`](examples/compose.yaml). Put real values in an
environment file or your secrets manager, not in the compose file.

```bash
docker compose -f examples/compose.yaml up -d
docker compose -f examples/compose.yaml logs -f
```

### Coolify

Add the service from `examples/compose.yaml` to a Docker Compose resource, or
paste it as a second service into an existing one. Coolify shows every
`${VAR:?}` as a required variable and blocks the deploy until it is set. If the
LDAP server runs in the same compose resource, set `LDAP_URL` to its service
name and port (e.g. `ldaps://ldap:636`) and `LDAP_TLS_NAME` to the name on its
certificate.

If several services in the resource share variable names, prefix this
service's Coolify variables (e.g. `LDAP_URL: ${SYNC_LDAP_URL:?}`).

### Tailscale or Headscale

With `TS_AUTHKEY` set, the container runs `tailscaled` in userspace networking
mode. It needs no `NET_ADMIN`, no `/dev/net/tun`, and doesn't change the host's
routing. Printer traffic, and only printer traffic, goes through its HTTP proxy
on `localhost:1055`. LDAP traffic does not use the tailnet.

1. On the printer's network, run a subnet router that advertises the printer,
   e.g. `tailscale set --advertise-routes=192.0.2.10/32`.
2. Approve that route in the Tailscale admin console or in Headscale.
3. Create an auth key for this container: non-reusable, not ephemeral, short
   expiry. For Headscale, check `headscale preauthkeys create --help`. The
   flags differ between versions. For example:
   `headscale preauthkeys create --user <user> --expiration 24h`.
4. Set `TS_AUTHKEY` (and `TS_LOGIN_SERVER` for Headscale) and deploy.

The key is only used for the first login. After that the node key in
`/var/lib/tailscale` is reused, so the key can expire.

### Without Tailscale

Leave `TS_AUTHKEY` unset. The worker connects to each printer's `host`
directly, so the container must be able to reach it (for example, run it on a
machine in the same building).

### Adding a printer

Add an object to `PRINTERS`, set `RICOH_PASSWORD_<NAME>` if its password
differs, and, with Tailscale, advertise and approve a route to it. Restart the
container and run the connectivity check.

## Connectivity check

The check changes nothing. It reports the tailnet state, the subnet route for
each printer, whether each printer's web page answers, and LDAP DNS, TLS, bind
and search results. It prints counts only, never names or emails.

```bash
docker exec <container> python -m ricoh_ldap_sync.check
```

Debugging tools in the image: `curl`, `openssl`, `dig`, `ldapsearch`, `nc`, and
`tailscale`, which is already pointed at the container's daemon:

```bash
docker exec <container> tailscale status
docker exec <container> curl -sI -x http://localhost:1055 http://192.0.2.10/
```

## Operations

- **Rotate the LDAP or printer password:** update the variable and redeploy.
- **Rotate the Tailscale node:** remove the node in the admin console or
  Headscale, empty the `/var/lib/tailscale` volume, set a fresh `TS_AUTHKEY`,
  and redeploy.
- **Reset ownership state:** stop the container and delete `/data/state.json`.
  On the next run nothing is deleted, and existing entries are adopted only
  when their name and email match exactly.

## Tested printers

| Model | Firmware | Result |
| --- | --- | --- |
| Ricoh IM C2510 | as found | Testing in progress |

Some newer Ricoh models reject address book writes over SOAP with
`UDIRECTORY_PERMISSION_DENIED` while still allowing deletes. Usually the account
lacks the *user administrator* role. On some models, writes over SOAP are
restricted entirely.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `route <printer>: no peer has an approved route` | The subnet router isn't advertising the printer's address, or the route isn't approved. |
| `printer <name>: GET ... failed` with Tailscale on | Same as above, or the subnet router is offline. Run `tailscale status` in the container. |
| `ldap tls: certificate not valid for <name>` | `LDAP_TLS_NAME` doesn't match the certificate. Set it to the name on the certificate. |
| `ldap bind ... invalidCredentials` | Wrong bind DN or password. |
| `UDIRECTORY_PERMISSION_DENIED` | The printer account isn't a user administrator, or the model blocks SOAP writes. |
| Address book locked on the panel | A session was left open. It ends when its time limit runs out. Restarting the printer also clears it. |
| `tailscale did not reach Running` at startup | Wrong `TS_LOGIN_SERVER`, or the auth key is expired or already used and there is no saved node state. |

## Acknowledgements

The SOAP protocol was learned by reading
[RicohAddressBook](https://github.com/desjardinsm/RicohAddressBook)
(Matthew Desjardins, based on work by Alexander Krause) and then confirmed
against a real printer. No code or templates were copied: that project doesn't
publish a license.

## License

[MIT](LICENSE)
