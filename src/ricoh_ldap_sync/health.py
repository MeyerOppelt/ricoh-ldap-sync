"""Container healthcheck: exit 0 when healthy, 1 otherwise."""

from __future__ import annotations

import os
import sys
import time

from . import config, tailnet

LAST_SUCCESS = "/data/last_success"


def main() -> int:
    if os.environ.get("TS_AUTHKEY", "").strip():
        try:
            state = tailnet.status().get("BackendState")
        except tailnet.TailnetError as e:
            print(e)
            return 1
        if state != "Running":
            print(f"tailscale state {state}")
            return 1
    if os.path.exists(LAST_SUCCESS):
        try:
            interval = int(os.environ.get("SYNC_INTERVAL_MINUTES") or config.DEFAULT_INTERVAL_MINUTES)
        except ValueError:
            interval = config.DEFAULT_INTERVAL_MINUTES
        age = time.time() - os.path.getmtime(LAST_SUCCESS)
        if age > 3 * interval * 60:
            print(f"last successful sync was {int(age // 60)} minutes ago")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
