# ricoh-ldap-sync

[![test](https://github.com/MeyerOppelt/ricoh-ldap-sync/actions/workflows/test.yml/badge.svg)](https://github.com/MeyerOppelt/ricoh-ldap-sync/actions/workflows/test.yml)
[![build](https://github.com/MeyerOppelt/ricoh-ldap-sync/actions/workflows/build.yml/badge.svg)](https://github.com/MeyerOppelt/ricoh-ldap-sync/actions/workflows/build.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Keeps the local address book on Ricoh multifunction printers in sync with the
users in an LDAP directory.

## Why

Scan-to-email only works well when the printer's address book is current. Most
offices maintain it by hand in Web Image Monitor: someone adds new staff, nobody
removes leavers, and names drift from the directory. Ricoh printers can search
an LDAP server from the panel, but that is slower to use than the local address
book and doesn't give you the index tabs (AB, CD, ...) people are used to.

`ricoh-ldap-sync` treats your directory as the source of truth and keeps each
printer's local address book matching it: new users appear, renamed users are
updated, and users who leave are removed. It is careful about what it touches
(see [Safety](#safety)), and it starts in dry-run mode so you can review exactly
what it would change.

It runs as one small container. It can reach printers on other networks through
an embedded Tailscale client (also works with a self-hosted Headscale server).
So it can run in the cloud next to your directory and still reach a printer in
an office, without a VPN on the host or any inbound firewall rules.

**Quick start:** follow [Setup, step by step](#setup-step-by-step). In short:
deploy with `DRY_RUN=true` (the default), check the plan in the logs, run the
[probe](#7-prove-the-printer-accepts-writes) once, then set `DRY_RUN=false`.

## Contents

- [How it works](#how-it-works)
- [Safety](#safety)
- [Requirements](#requirements)
- [Setup, step by step](#setup-step-by-step)
- [Configuration reference](#configuration-reference)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Tested printers](#tested-printers)
- [Development](#development)
- [Protocol reference](docs/protocol.md)

## How it works

```
 ┌──────────────── container ────────────────┐
 │                                           │   LDAPS (direct)    ┌──────────────┐
 │  worker ──────────────────────────────────┼────────────────────►│ LDAP server  │
 │    │                                      │                     └──────────────┘
 │    │ HTTP via proxy localhost:1055        │
 │    ▼                                      │   tailnet           ┌──────────────┐   LAN   ┌─────────┐
 │  tailscaled (userspace, optional) ────────┼────────────────────►│ subnet router├────────►│ printer │
 │                                           │                     └──────────────┘         └─────────┘
 └───────────────────────────────────────────┘
```

- **Two processes in one container:** `tailscaled` (only when `TS_AUTHKEY` is
  set) and the worker. `tini` is PID 1. If either process exits, the container
  exits and your orchestrator restarts it.
- **Printer traffic goes through Tailscale; nothing else does.** `tailscaled`
  runs in userspace networking mode with an HTTP proxy on `localhost:1055`. The
  worker passes that proxy only on the HTTP session it uses for printers. There
  is no global `HTTP_PROXY`. Userspace mode needs no `NET_ADMIN` and no
  `/dev/net/tun`, and doesn't touch the host's routing.
- **LDAP traffic goes straight to the server**, for example over a Docker
  network to an LDAP container in the same compose project.
- **The printer protocol is Ricoh's address book SOAP service**
  (`/DH/udirectory`), the interface Ricoh's own management tools use. The web
  UI is never scraped.

Each run:

1. Reads users from LDAP: `LDAP_BASE`, `LDAP_FILTER`. The display name is the
   first of `LDAP_ATTR_NAME` that has a value. Emails are compared lowercased.
2. For each printer, opens an address book session, reads the entries, and
   works out the changes:
   - **Add** each user without an entry: name, key display, the Title1 index tag
     for the first letter, and the email as a scan-to-email destination. Not a
     sender, no folder, no fax.
   - **Update** entries whose user's display name changed in LDAP.
   - **Delete** entries whose user is no longer in LDAP.
3. Closes the session in every case, even after an error. Sessions use a short
   time limit, because an open session can lock the address book on the panel.
4. Writes `/data/last_success`, which the container healthcheck reads.

Printers sync independently: one failing doesn't block the others. When a
printer's address book is full, adds stop and an error is logged.

### Keeping entries in alphabetical order

The printer lists entries by registration number, and gives each new entry the
next free number. So someone who joins later appears at the end of their index
tab, not in alphabetical position. Each run adds new people in name order, so a
batch of new entries is always alphabetical. Over time, though, the order drifts.

With `KEEP_SORTED=true`, the sync keeps its own entries in alphabetical order:

1. After a run's changes, it checks whether its entries' registration numbers
   follow name order (ignoring case and accents).
2. If they don't, it adds a fresh copy of each of its entries in name order, so
   the printer hands out ascending numbers.
3. Once every copy is confirmed, it deletes the old entries.

Adding copies first means nobody disappears from the panel if something fails.
If a copy can't be added, the copies made so far are removed and the old
entries stay as they were. If the container stops partway through, the next
run finishes or undoes the job. A removal never triggers a renumber, and
neither does a new person whose name sorts last.

Things to know before turning it on:

- **Only the sync's own entries are renumbered.** Hand-made entries keep their
  numbers, so they stay where they are among the sorted ones.
- **A renumber rewrites every sync-owned entry.** Each one gets a new
  registration number and ID, and printer-side data such as "date last used"
  starts again. Changes someone made on the panel to a sync-owned entry (a
  folder, a user code) are lost.
- **Copies need room.** The address book must have space for one copy of every
  sync-owned entry at the same time. If `capacity` is set and there isn't
  enough room, the renumber is skipped with an error.
- **Brief duplicates.** During a renumber, which takes a few seconds, the panel
  can briefly show each person twice.

## Safety

The sync is built so that a misconfiguration or outage leaves the printer
alone rather than emptying it.

- **It only touches entries it created.** Every entry it creates is recorded in
  `/data/state.json` (printer, email, entry ID, name written, last sync). It only
  ever updates or deletes entries listed there. Entries people created by hand
  are never changed.
- **No duplicates of manual entries.** If a hand-made entry already has a user's
  email, the sync leaves it alone, doesn't add a second one, and logs one
  warning per run.
- **A missing state file never causes deletes.** Without a state file nothing
  is deleted. An existing entry is adopted only when its name and email exactly
  match what the sync would write, and each adoption is logged.
- **LDAP errors skip the run.** If the LDAP query fails for any reason
  (connection refused, bind failure, size limit, timeout), the whole run is
  skipped for every printer. An error is never treated as "zero users".
- **Safety brake.** For a printer, all deletes are skipped and an error is
  logged if LDAP returned 0 users, or if the deletes would remove more than
  `MAX_DELETE_FRACTION` (default `0.3`) of that printer's managed entries. Adds
  and updates still go ahead.
- **Dry run by default.** With `DRY_RUN=true` (the default) the worker logs the
  plan (adds, updates and deletes for each printer) and writes nothing.

## Requirements

**Printer**
- A Ricoh MFP with Web Image Monitor over HTTP. See
  [Tested printers](#tested-printers).
- An account that can edit the address book. On Ricoh this is the *user
  administrator* role, which can be a different account from the machine
  administrator. On many printers one `admin` account has every role.

**LDAP**
- A bind account with read access to your users. Read-only is enough.
- Users with an email attribute (`mail` by default) and a name attribute
  (`displayName`, `cn` or `uid` by default).
- LDAPS recommended. Plain `ldap://` works, but sends the bind password
  unencrypted.

**Host**
- Docker on `linux/amd64` or `linux/arm64`.
- Either a network path to each printer, or a Tailscale/Headscale tailnet with
  a subnet router at each printer's site.

## Setup, step by step

### 1. Create the LDAP bind account

Create a dedicated, read-only account for the sync, for example
`uid=printer-sync,ou=people,dc=example,dc=com`. Don't reuse the account the
printers use for panel search, so you can rotate or revoke it separately.

To check what the directory returns, run this from any machine that can reach
it (it needs `ldap-utils`):

```bash
ldapsearch -H ldaps://ldap.example.com:636 -D 'uid=printer-sync,ou=people,dc=example,dc=com' -W \
  -b 'ou=people,dc=example,dc=com' '(&(uid=*)(mail=*))' uid cn displayName mail
```

### 2. Check the printer account

Log in to Web Image Monitor as the account you'll use and check that you can
edit the address book (**Device Management > Address Book**). If you can't,
give the account the user administrator role, or use the account that has it.

### 3. Give the container a path to each printer

Pick one of the two options below.

#### Option A: direct (no Tailscale)

If the container's host can reach the printers (for example it runs on a
server in the same building), leave `TS_AUTHKEY` unset. The worker connects to
each printer's `host` directly. Skip to step 4.

#### Option B: through a Tailscale or Headscale tailnet

You need a subnet router on each printer's network. That's any machine running
Tailscale that can reach the printer. Then:

1. **Advertise the printer's address** from the subnet router. A `/32` limits
   it to the one device:

   ```bash
   tailscale set --advertise-routes=192.0.2.10/32
   ```

   `tailscale set` changes only the flags you give, so an existing exit-node
   setting stays.

2. **Approve the route.** In the Tailscale admin console, open the router's
   machine page > **Edit route settings**. On Headscale (0.26 and later):

   ```bash
   headscale nodes list-routes
   headscale nodes approve-routes --identifier <router-id> --routes <every route to approve, comma-separated>
   ```

   `--routes` replaces the node's approved list, so include routes that are
   already approved (for example `0.0.0.0/0,::/0` for an exit node). Older
   Headscale versions use `headscale routes list` and
   `headscale routes enable -r <id>`.

3. **Allow it in your ACL policy, if you have one.** With no policy, everything
   is allowed and you can skip this. Once a policy exists, the tailnet is
   deny-by-default. The control server also *hides* a subnet route from any node
   the policy doesn't let reach it, so a missing rule looks like an unapproved
   route. Three common surprises:
   - A destination like `tag:servers:*` covers those machines' own tailnet
     addresses, **not** the subnets they route. Name the printer's address
     instead.
   - `autogroup:self` covers devices owned by a user, never tagged devices.
   - `autogroup:internet` is exit-node traffic and excludes private ranges.

   A minimal rule, giving the sync node its own tag:

   ```json
   {
     "hosts": { "office-printer": "192.0.2.10/32" },
     "tagOwners": { "tag:printer-sync": ["group:admin"] },
     "acls": [
       { "action": "accept", "src": ["tag:printer-sync"], "dst": ["office-printer:80"] }
     ]
   }
   ```

   Port 80 covers both Web Image Monitor and the address book service.

4. **Create an auth key for the container:** non-reusable, not ephemeral,
   short expiry. Give it your sync tag if you use one. On Headscale, flags
   differ between versions, so check `headscale preauthkeys create --help`:

   ```bash
   headscale preauthkeys create --user <user> --expiration 24h
   ```

   The key is only used for the container's first login. After that the node
   identity in `/var/lib/tailscale` is reused, so the key can expire.

5. **For Headscale, set `TS_LOGIN_SERVER`** to your server's URL, e.g.
   `https://headscale.example.com`. The container refuses to start if it sees a
   Headscale key (`hskey-...`) without `TS_LOGIN_SERVER`, rather than sending
   the key to Tailscale's servers.

### 4. Configure

All settings are environment variables. They're listed in full in the
[Configuration reference](#configuration-reference). The required ones are
`LDAP_URL`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `LDAP_BASE`, `PRINTERS`, and
`RICOH_PASSWORD` (or a per-printer `RICOH_PASSWORD_<NAME>`).

`PRINTERS` is a JSON list. Each printer has a `name` and a `host`:

```json
[{"name":"office","host":"192.0.2.10"},{"name":"warehouse","host":"192.0.2.20"}]
```

**Choose printer names carefully.** The sync records which entries it owns
under each name, so renaming a printer later means it no longer recognises its
own entries there. To recover from that, see
[Reset ownership state](#reset-ownership-state).

The container checks every variable at startup. If anything is missing or
malformed, it exits with a list naming each bad variable. Secret values are
never printed.

### 5. Deploy

#### Docker Compose

Start from one of the examples, and put real values in an `.env` file next to
it or in your secrets manager, not in the compose file:

- [`examples/compose.yaml`](examples/compose.yaml): the sync on its own, for
  any LDAP server it can reach.
- [`examples/compose.with-azuread-ldap-wrapper.yaml`](examples/compose.with-azuread-ldap-wrapper.yaml):
  the sync next to [AzureAD-LDAP-wrapper](https://github.com/ahaenggli/AzureAD-LDAP-wrapper),
  which serves Microsoft Entra ID users over LDAPS. Both services have
  healthchecks, and the sync waits for the wrapper to be healthy before
  starting.

```bash
docker compose up -d
docker compose logs -f ricoh-ldap-sync
```

Always reference an exact image version, e.g.
`ghcr.io/meyeroppelt/ricoh-ldap-sync:0.2.0`, never `latest`. That way an
upgrade happens only when you change the tag.

#### Coolify

1. Add the service to a **Docker Compose** resource. If your LDAP server is
   already a compose resource in Coolify, paste this service into that same
   resource as a second service. They then share a network, and the sync can
   reach LDAP by its service name.
2. Coolify lists every `${VAR}` from the compose file under **Environment
   Variables**. Variables written `${VAR:?}` are required, and Coolify blocks
   the deploy until they're set. Variables written `${VAR:-default}` show their
   default.
3. If the resource already has variables with the same names (for example an
   LDAP container using `LDAP_URL`), map them to prefixed Coolify variables:

   ```yaml
   environment:
     LDAP_URL: ${SYNC_LDAP_URL:-ldaps://ldap:636}
     LDAP_BIND_PASSWORD: ${SYNC_LDAP_BIND_PASSWORD:?}
   ```

4. Click **Deploy**. Deploying a compose resource restarts **every** service in
   it, so an LDAP server in the same resource is briefly unavailable. Give the
   LDAP service a healthcheck, and make the sync depend on it with
   `condition: service_healthy` (as in the wrapper example). The sync then
   starts only once LDAP is ready. Without that, the startup check still waits
   up to 120 s for LDAP, and the sync skips any run where LDAP is down. Coolify
   shows each service's health next to its status.

**Reaching LDAP in the same compose project.** Set `LDAP_URL` to the
container's service name and internal port, for example
`ldaps://ldap:13389`. Set `LDAP_TLS_NAME` to the name on its certificate, for
example `ldap.example.com`. The connection goes over the Docker network, while
the certificate is still validated against its real name. This also avoids
problems with a public LDAP port that is firewalled to particular addresses.

**Volumes.** Both `/data` and `/var/lib/tailscale` must be persistent. Bind
mounts (`./data:/data`) and named volumes both work. Docker creates bind-mount
directories owned by root, so the container starts as root, makes the two
directories writable by UID 1000, and then runs `tailscaled` and the worker as
UID 1000.

### 6. Verify the connection

On every start the container runs a read-only connectivity check and prints
the result in its logs. You can run it again at any time:

```bash
docker exec <container> python -m ricoh_ldap_sync.check
```

In Coolify, use the service's **Terminal** tab and run
`python -m ricoh_ldap_sync.check`.

A healthy result looks like this:

```
INFO config: 1 printer(s): office=192.0.2.10; LDAP ldaps://ldap:13389 (certificate name ldap.example.com)
PASS data volume: /data is writable
PASS tailscale: state Running, node ricoh-ldap-sync 100.64.0.5, fd7a:115c:a1e0::5
PASS tailscale accept-routes: True
PASS route office: 192.0.2.10 via office-router (192.0.2.10/32)
PASS printer office: HTTP 200, final path /, title 'Web Image Monitor'
PASS ldap dns: ldap -> 172.18.0.2
PASS ldap tls: certificate valid for ldap.example.com, issuer Let's Encrypt, expires Jan 26 21:35:21 2027 GMT
PASS ldap bind: bound as uid=printer-sync,ou=people,dc=example,dc=com
PASS ldap search: 24 entries, 24 usable users; entries with each attribute: displayName=24, cn=24, uid=24, mail=24
RESULT: all checks passed
```

| Line | Checks |
| --- | --- |
| `data volume` | `/data` is writable, so the state file can be saved. |
| `tailscale` | `tailscaled` is logged in and Running. Only shown when `TS_AUTHKEY` is set. |
| `tailscale accept-routes` | The node accepts subnet routes. |
| `route <printer>` | A peer has an approved route covering the printer's IP, and is online. |
| `printer <printer>` | The printer's web page answers through the proxy, or directly when Tailscale is off. |
| `ldap dns` / `ldap tls` / `ldap bind` | Name resolution, the certificate validated against `LDAP_TLS_NAME`, and the bind account. |
| `ldap search` | How many entries the filter returns, how many are usable (they have an email and a name), and how many have each attribute. Use this to choose `LDAP_ATTR_NAME`. |

The check prints counts only. It never prints names, email addresses or
secrets.

The image also includes `curl`, `openssl`, `dig`, `ldapsearch`, `nc` and `ps`
for debugging. `tailscale` is already pointed at the container's daemon:

```bash
docker exec <container> tailscale status
docker exec <container> curl -sI -x http://localhost:1055 http://192.0.2.10/
```

### 7. Prove the printer accepts writes

Some Ricoh models refuse address book writes over SOAP, so check yours before
going live. The probe is read-only unless you pass `--write-test`:

```bash
docker exec <container> python -m ricoh_ldap_sync.probe               # read only
docker exec <container> python -m ricoh_ldap_sync.probe --write-test  # add, rename, delete a test entry
```

The read-only run:
1. Opens a shared session.
2. Lists the address book.
3. Prints every property of the first two entries.
4. Summarises the name lengths and index tags in use.

`--write-test` then:
1. Adds `ZZ Sync Test` with `sync-test@invalid.example`.
2. Reads it back.
3. Renames it with a 48-character name, to show what the printer stores. That
   tells you the real length limits.
4. Deletes it, and checks that it's gone.

It never touches any other entry. If a previous test was interrupted, it first
removes leftover entries that have both that name prefix and that address.
Output masks names (`Axxxx Axxxxx`, same length) and replaces email addresses.
Add `--printer NAME` to pick a printer, and `--save DIR` to keep the scrubbed raw
responses.

If `add` fails with `UDIRECTORY_PERMISSION_DENIED`, see
[Tested printers](#tested-printers).

### 8. Review the dry run, then go live

With `DRY_RUN=true`, each run logs one line per planned change, then a summary:

```
INFO printer=office action=add email=alice@example.com name="Alice Example" key_display="Alice Example" result=planned
INFO printer=office action=update email=bob@example.com entry=12 name="Robert Example" was="Bob Example" result=planned
INFO printer=office action=delete email=carol@example.com entry=7 name="Carol Example" result=planned
WARNING printer=office action=skip email=dave@example.com entry=3 result=a manually created entry already has this email; leaving it alone
INFO printer=office plan: 1 add, 1 update, 1 delete, 0 adopt, 20 unchanged, 1 manual (dry run, nothing written)
```

Check that the adds are the people you expect. You can also preview a run at
any time, even when live, without writing anything:

```bash
docker exec <container> python -m ricoh_ldap_sync --once --dry-run
```

**Going live:**

1. The probe's write test passed (step 7).
2. The dry-run plan adds the people you expect, and the `skip` warnings are
   hand-made entries you want to keep.
3. Set `DRY_RUN=false` and redeploy. The first live run deletes nothing: there
   is no ownership state yet, so it only adds, and adopts exact matches.
4. Check the new entries on the printer's panel.
5. Run `docker exec <container> python -m ricoh_ldap_sync --once`, or wait for
   the next run. It should log `0 add, 0 update, 0 delete`.
6. Remove a test user from the directory, wait for the LDAP server to pick up
   the change, and run once more. Their entry should be deleted.

## Configuration reference

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `LDAP_URL` | yes | | `ldaps://host[:port]` or `ldap://host[:port]`, e.g. `ldaps://ldap.example.com:636`. |
| `LDAP_TLS_NAME` | no | host from `LDAP_URL` | Name the server certificate must be valid for. Lets you connect to a container name while validating the certificate's real name. |
| `LDAP_BIND_DN` | yes | | |
| `LDAP_BIND_PASSWORD` | yes | | |
| `LDAP_BASE` | yes | | Search base, e.g. `ou=people,dc=example,dc=com`. |
| `LDAP_FILTER` | no | `(&(uid=*)(mail=*))` | Which entries are users. |
| `LDAP_ATTR_NAME` | no | `displayName,cn,uid` | Name attributes, tried in order. |
| `LDAP_ATTR_MAIL` | no | `mail` | Email attribute. |
| `PRINTERS` | yes | | JSON list of `{"name","host"}`, plus an optional `"capacity"` (the most entries the address book holds, for example `2000`). Without it, adds stop at the first error a full book returns. Names: letters, digits, `-`, `_`. Host: name or IP, optional `:port`, IPv6 in brackets. |
| `RICOH_USER` | no | `admin` | Printer account with the user administrator role. |
| `RICOH_PASSWORD` | yes* | | Used for every printer without its own override. *Optional if every printer has one. |
| `RICOH_PASSWORD_<NAME>` | no | | Per-printer password. `<NAME>` is the printer name uppercased, with `-` replaced by `_` (`office-2` → `RICOH_PASSWORD_OFFICE_2`). |
| `TS_AUTHKEY` | no | | Tailscale/Headscale auth key. If unset, Tailscale doesn't start and printers are reached directly. |
| `TS_LOGIN_SERVER` | no | Tailscale's default | Required for Headscale, e.g. `https://headscale.example.com`. |
| `TS_HOSTNAME` | no | `ricoh-ldap-sync` | Node name on the tailnet. |
| `SYNC_INTERVAL_MINUTES` | no | `15` | Minutes between runs. |
| `DRY_RUN` | no | `true` | `true` logs the plan only. `false` writes to printers. |
| `KEEP_SORTED` | no | `false` | `true` re-adds the sync's entries in name order whenever their registration numbers are out of order. See [Keeping entries in alphabetical order](#keeping-entries-in-alphabetical-order). |
| `MAX_DELETE_FRACTION` | no | `0.3` | Safety brake threshold, 0 to 1. |
| `LOG_LEVEL` | no | `info` | `debug`, `info`, `warning` or `error`. |

| Volume | Holds | If lost |
| --- | --- | --- |
| `/data` | `state.json` (which entries the sync owns) and `last_success`. | Nothing is deleted. Existing entries are re-adopted only on an exact match. |
| `/var/lib/tailscale` | The node identity. | The container needs a new `TS_AUTHKEY` and joins as a new node. |

Image tags: `X.Y.Z` (exact release, use this), `X.Y` (latest patch of a minor
version) and `sha-<commit>`. There is no `latest` tag.

## Operations

**Logs.** One line per action (printer, action, email, result). The
tool never logs passwords or the auth key. `tailscaled` is chatty: lines
starting `magicsock:`, `wgengine:`, `dns:` and `netmap:` are normal.

**Health.** The image has a healthcheck (`python -m ricoh_ldap_sync.health`),
and the examples repeat it so it's visible in the compose file. It fails if
Tailscale is enabled but not Running, if no run has succeeded yet, or if the
last successful run is older than 3 × `SYNC_INTERVAL_MINUTES`. A run succeeds
when LDAP was read and every printer synced without an error. Docker and Coolify
report an unhealthy container but don't restart it. The container does exit,
and get restarted, if `tailscaled` or the worker stops.

**Run a sync now.** The worker runs every `SYNC_INTERVAL_MINUTES`. To run one
immediately, for example after changing the directory:

```bash
docker exec <container> python -m ricoh_ldap_sync --once            # follows DRY_RUN
docker exec <container> python -m ricoh_ldap_sync --once --dry-run  # never writes
```

The exit code is 0 if LDAP and every printer succeeded. A lock in `/data`
makes a manual run wait for a scheduled run in progress, and the other way
round, so two runs never overlap.

**Approve deletes blocked by the safety brake.** The brake blocks the same
deletes on every run until someone looks. If they are legitimate (for example,
several people left at once, or a small directory where one leaver is more than
`MAX_DELETE_FRACTION`), check the `blocked by safety brake` lines in the log,
then run once with a higher limit:

```bash
docker exec <container> env MAX_DELETE_FRACTION=1 python -m ricoh_ldap_sync --once
```

This applies to that one run only. The brake for 0 LDAP users can't be
overridden.

**Upgrade.** Read the [changelog](CHANGELOG.md), change the image tag, and
redeploy. `python -m ricoh_ldap_sync --version` shows the running version.

**Add a printer.**
1. Add it to `PRINTERS`, and set `RICOH_PASSWORD_<NAME>` if its password differs.
2. With Tailscale: advertise and approve a route to it, and allow it in your
   ACL policy (step 3).
3. Redeploy, and check that the startup check shows `PASS route` and
   `PASS printer` for it.
4. Leave `DRY_RUN=true` for a run to review its plan, if you like.

**Rotate secrets.**
- *LDAP or printer password:* change it at the source, update the variable,
  and redeploy.
- *Tailscale node:* delete the node in the admin console or Headscale, empty
  the `/var/lib/tailscale` volume, set a fresh `TS_AUTHKEY`, and redeploy.

<a id="reset-ownership-state"></a>
**Reset ownership state.** Stop the container, delete `/data/state.json`, and
start it. Nothing is deleted on the next run. Existing entries are adopted only
where name and email match exactly, and each adoption is logged.

**Remove the sync.** Stop the container. The entries it created stay on the
printers. Delete them in Web Image Monitor if you no longer want them.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Container exits with `Invalid configuration:` | The message lists each missing or malformed variable. Fix those and redeploy. |
| `TS_AUTHKEY is a Headscale key but TS_LOGIN_SERVER is not set` | Set `TS_LOGIN_SERVER` to your Headscale URL. |
| `tailscale up failed` or `did not reach Running` | Wrong `TS_LOGIN_SERVER`, or the key is expired or already used and there's no saved node state. Create a new key. |
| `FAIL route <printer>: no peer has an approved route` | The subnet router isn't advertising the address, the route isn't approved, or the ACL policy doesn't allow this node to reach it. A policy also hides the route, so check all three (step 3). |
| `FAIL printer <printer>: ... ReadTimeout` with Tailscale on | With no tailnet route, the proxy tries the address on the host's own network, where it goes nowhere. Same causes as the row above. |
| `FAIL printer <printer>: ... ConnectTimeout` with Tailscale off | The host can't reach the printer. Check the IP, the network, and that Web Image Monitor is enabled. |
| `FAIL ldap reachable` or `ldap tls: ... Connection refused` | The LDAP server isn't listening yet (for example, just restarted by a deploy), or the host and port are wrong. Run the check again after a minute. |
| `FAIL ldap dns` | The name in `LDAP_URL` doesn't resolve. For a container in the same compose project, use its service name and check both services are in the same project. |
| `ldap tls: certificate not valid for <name>: Hostname mismatch` | `LDAP_TLS_NAME` doesn't match the certificate. Set it to the name the certificate was issued for. |
| `ldap tls: ... unable to get local issuer certificate` or `certificate has expired` | The certificate isn't publicly trusted (self-signed or private CA) or has expired. Use a publicly trusted certificate, for example from Let's Encrypt, and renew it. |
| `ldap bind ... invalidCredentials` | Wrong bind DN or password. |
| `ldap search: 0 entries` | Wrong `LDAP_BASE` or `LDAP_FILTER`, or the bind account can't read users. |
| `FAIL data volume` | `/data` isn't writable. Check the volume mount. |
| `UDIRECTORY_PERMISSION_DENIED` | The printer account lacks the user administrator role, or the model blocks address book writes over SOAP. See [Tested printers](#tested-printers). |
| `safety brake: N of M managed entries would be deleted` | More deletes than `MAX_DELETE_FRACTION` allows. If they're right, see [Approve deletes blocked by the safety brake](#operations). If not, check `LDAP_FILTER` and the directory's group membership. |
| `safety brake: LDAP returned 0 users` | The filter or base matches nobody, or the directory was emptied. Nothing is deleted until users come back. |
| `action=skip ... a manually created entry already has this email` | Someone added this person by hand. Delete that entry in Web Image Monitor if you want the sync to manage them. It's added on the next run. |
| `run finished with errors` every run, container unhealthy | Look for the `ERROR` line above it: a printer that's off or unreachable, LDAP down, or a refused write. |
| `renumber skipped: needs room for N temporary copies` | The address book can't hold a copy of every sync-owned entry at once. Raise `capacity` if it's set too low, or free space. |
| `renumber stopped: ...; removing the copies` | A copy couldn't be added (for example a refused write). Everyone keeps their old entry. The next run tries again. |
| `renumber: deleting the old copies failed` | The new copies are recorded. The next run deletes the old ones (logged as `action=retire`). |
| Address book locked on the panel | An address book session was left open. It ends when its time limit runs out. Restarting the printer also clears it. |

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

The container holds each printer's admin password, the LDAP bind password, and
(with Tailscale) a node key. So:

- Run it on a host you trust, and keep its volumes private to that host.
- Give the LDAP account read-only access.
- With Tailscale, give the node its own tag and an ACL that allows only the
  printers on port 80.
- Printer traffic is plain HTTP, as Web Image Monitor serves it. With Tailscale
  it's encrypted between the container and the subnet router. It's plain only
  on the printer's own LAN.

## Tested printers

| Model | Firmware | Result |
| --- | --- | --- |
| Ricoh IM C2510 | as found | Connectivity and Web Image Monitor verified. Address book reads and writes not yet confirmed: run the probe. |

**Known risk:** some newer Ricoh models reject address book writes over SOAP
(`putObjects`) with `UDIRECTORY_PERMISSION_DENIED`, while deletes still work.
Usually the account lacks the user administrator role. On some models, writes
over SOAP are restricted entirely. If you test another model, please open an
issue with the result.

## Development

```bash
python3.15 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
docker build -t ricoh-ldap-sync:dev .
```

Tests need no network, printer or LDAP server. `tests/fakeprinter.py` is an
in-memory address book that answers the same SOAP calls as a printer, with
made-up names and `@example.com` addresses. Recordings from real devices go in
`recordings/`, which git ignores. See [CONTRIBUTING.md](CONTRIBUTING.md) and
the [protocol reference](docs/protocol.md).

Layout:

| Path | Purpose |
| --- | --- |
| `src/ricoh_ldap_sync/config.py` | Environment parsing and validation |
| `src/ricoh_ldap_sync/ldap_source.py` | LDAP users, with certificate name override |
| `src/ricoh_ldap_sync/ricoh_soap.py` | Address book SOAP client |
| `src/ricoh_ldap_sync/sync.py` | Plan (diff, safety brake, ownership), state file, applying changes |
| `src/ricoh_ldap_sync/main.py` | Worker loop, `--once`, `--dry-run`, run lock |
| `src/ricoh_ldap_sync/check.py` | Connectivity check |
| `src/ricoh_ldap_sync/probe.py` | Protocol probe |
| `src/ricoh_ldap_sync/health.py` | Container healthcheck |
| `entrypoint.sh` | Volume ownership, Tailscale startup, process supervision |

**Releases.** Pushing a `vX.Y.Z` tag runs the tests, then builds
`linux/amd64` and `linux/arm64` images and pushes them to
`ghcr.io/meyeroppelt/ricoh-ldap-sync` as `X.Y.Z`, `X.Y` and `sha-<commit>`. Pull
requests and pushes to `main` run the tests. Dependabot proposes weekly updates
for actions, Python packages and base images. Every version is pinned.

## Acknowledgements

The SOAP protocol was learned by reading
[RicohAddressBook](https://github.com/desjardinsm/RicohAddressBook)
(Matthew Desjardins, based on work by Alexander Krause) and then confirmed
against a real printer. No code or templates were copied: that project doesn't
publish a license.

## License

[MIT](LICENSE)
