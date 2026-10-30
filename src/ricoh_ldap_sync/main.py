"""Worker: run a sync at start, then every SYNC_INTERVAL_MINUTES.

    python -m ricoh_ldap_sync              # the container's worker loop
    python -m ricoh_ldap_sync --once       # one sync now, then exit
    python -m ricoh_ldap_sync --once --dry-run   # preview one sync, write nothing
    python -m ricoh_ldap_sync --once --renumber  # one sync, then re-add entries in name order from the lowest number
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager

from . import __version__, check, config, graph_source, ldap_source, sync
from .ricoh_soap import RicohClient, RicohError

log = logging.getLogger("ricoh_ldap_sync")

DATA_DIR = "/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LAST_SUCCESS = os.path.join(DATA_DIR, "last_success")
LOCK_FILE = os.path.join(DATA_DIR, ".sync.lock")


@contextmanager
def run_lock(path: str = LOCK_FILE):
    """Only one sync at a time, so a manual --once run can't overlap the worker."""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another sync is running; waiting for it to finish")
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_once(cfg: config.Config, state_path: str, force_renumber: bool = False) -> bool:
    """One sync of every printer. Returns True if the source and every printer succeeded."""
    source = graph_source if cfg.source == "graph" else ldap_source
    try:
        users = source.fetch_users(cfg)
    except (ldap_source.LdapError, graph_source.GraphError) as e:
        log.error("%s: %s; skipping this run for every printer", cfg.source, e)
        return False
    log.info("%s: %d users", cfg.source, len(users))
    if not users and cfg.source == "graph":
        log.error("graph: no member of group %s has an email address; nothing will be deleted", cfg.graph_group_id)
    elif not users:
        log.error("ldap: no usable users under %s matching %s (each needs %s and one of %s); nothing will be deleted",
                  cfg.ldap_base, cfg.ldap_filter, cfg.ldap_attr_mail, ", ".join(cfg.ldap_attr_name))

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
                                    capacity=printer.capacity, save=save, keep_sorted=cfg.keep_sorted,
                                    force_renumber=force_renumber)
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


def sync_now(cfg: config.Config, force_renumber: bool = False) -> bool:
    with run_lock(LOCK_FILE):
        ok = run_once(cfg, STATE_FILE, force_renumber)
    if ok:
        log.info("run complete")
    else:
        log.error("run finished with errors")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ricoh_ldap_sync", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one sync now and exit (0 if it succeeded)")
    parser.add_argument("--dry-run", action="store_true", help="log the plan without writing, whatever DRY_RUN says")
    parser.add_argument("--renumber", action="store_true",
                        help="with --once: delete and re-add the sync's entries in name order, even if already sorted")
    parser.add_argument("--version", action="version", version=f"ricoh-ldap-sync {__version__}")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if args.dry_run:
        cfg = dataclasses.replace(cfg, dry_run=True)
    if args.renumber and not args.once:
        print("--renumber only works with --once", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ", stream=sys.stdout)
    logging.Formatter.converter = time.gmtime
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _stop)

    mode = ("DRY RUN (nothing is written)" if cfg.dry_run else "LIVE") + (", keeping entries sorted" if cfg.keep_sorted else "")
    if args.once:
        log.info("ricoh-ldap-sync %s: one run, %d printer(s), %s", __version__, len(cfg.printers), mode)
        if args.renumber and cfg.dry_run:
            log.info("--renumber ignored: dry run writes nothing")
        return 0 if sync_now(cfg, force_renumber=args.renumber) else 1

    log.info("ricoh-ldap-sync %s starting: %d printer(s), every %d min, %s", __version__, len(cfg.printers),
             cfg.sync_interval_minutes, mode)
    check.main(["--wait", "120"])
    while True:
        started = time.monotonic()
        if sync_now(cfg):
            with open(LAST_SUCCESS, "w") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        time.sleep(max(cfg.sync_interval_minutes * 60 - (time.monotonic() - started), 0))


if __name__ == "__main__":
    sys.exit(main())
