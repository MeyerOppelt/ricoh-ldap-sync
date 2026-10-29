"""Worker: run a sync at start, then every SYNC_INTERVAL_MINUTES."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from . import check, config, ldap_source, sync
from .ricoh_soap import RicohClient, RicohError

log = logging.getLogger("ricoh_ldap_sync")

DATA_DIR = "/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LAST_SUCCESS = os.path.join(DATA_DIR, "last_success")


def run_once(cfg: config.Config, state_path: str = STATE_FILE) -> bool:
    """One sync of every printer. Returns True if LDAP and every printer succeeded."""
    try:
        users = ldap_source.fetch_users(cfg)
    except ldap_source.LdapError as e:
        log.error("ldap: %s; skipping this run for every printer", e)
        return False
    log.info("ldap: %d users", len(users))

    try:
        state = sync.load_state(state_path)
    except sync.StateError as e:
        log.error("state: %s; skipping this run. Fix or remove the file (see README: Reset ownership state)", e)
        return False
    if state is None:
        log.warning("state: %s not found; nothing will be deleted this run, and only exact matches are adopted",
                    state_path)
        state = {}
        missing = True
    else:
        missing = False

    ok = True
    for printer in cfg.printers:
        records = None if missing or printer.name not in state else state[printer.name]

        def save(new_records, name=printer.name):
            state[name] = new_records
            sync.save_state(state_path, state)

        client = RicohClient(printer.base_url, cfg.ricoh_user, printer.password, proxy=cfg.printer_proxy)
        try:
            ok &= sync.sync_printer(client, printer.name, users, records, dry_run=cfg.dry_run,
                                    max_delete_fraction=cfg.max_delete_fraction,
                                    capacity=printer.capacity, save=save)
        except RicohError as e:
            ok = False
            log.error("printer=%s error: %s", printer.name, e)
        except Exception:
            ok = False
            log.exception("printer=%s unexpected error", printer.name)
    return ok


def _stop(signum, frame):
    # Raising here unwinds through the open session's finally, which closes it.
    raise SystemExit(0)


def main() -> int:
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ", stream=sys.stdout)
    logging.Formatter.converter = time.gmtime
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _stop)

    log.info("starting: %d printer(s), every %d min, %s", len(cfg.printers), cfg.sync_interval_minutes,
             "DRY RUN (nothing is written)" if cfg.dry_run else "LIVE")
    check.main(["--wait", "120"])

    while True:
        started = time.monotonic()
        if run_once(cfg):
            with open(LAST_SUCCESS, "w") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
            log.info("run complete")
        else:
            log.error("run finished with errors")
        time.sleep(max(cfg.sync_interval_minutes * 60 - (time.monotonic() - started), 0))


if __name__ == "__main__":
    sys.exit(main())
