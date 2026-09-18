#!/usr/bin/env python3
"""Direct CLI for the tracker, without the API/web layer.

Usage (unchanged from before the refactor):
    track.py <NAME>              track a satellite or solar-system body
    track.py loc <LOCATOR>       point at the heading toward a Maidenhead locator
    track.py raw <AZ> <EL>       point at a raw azimuth/elevation

Mainly useful for quick debugging on the box the rotator is attached to;
day-to-day control is via the API/web UI (see anttrack.api / README).
"""

import sys
import time

from anttrack.config import load_config
from anttrack.tracker import Tracker, TrackerError


def main():
    if len(sys.argv) == 3 and sys.argv[1].lower() == "loc":
        mode, arg = "loc", sys.argv[2]
    elif len(sys.argv) == 4 and sys.argv[1].lower() == "raw":
        mode, arg = "raw", (float(sys.argv[2]), float(sys.argv[3]))
    elif len(sys.argv) == 2:
        mode, arg = "target", sys.argv[1]
    else:
        print("Usage: track.py <NAME> | loc <LOCATOR> | raw <AZ> <EL>")
        sys.exit(1)

    qth_cfg, rotator_cfg = load_config()
    tracker = Tracker(qth_cfg, rotator_cfg)

    try:
        if mode == "loc":
            tracker.start_loc(arg)
        elif mode == "raw":
            tracker.start_raw(*arg)
        else:
            tracker.start_target(arg)
    except TrackerError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    last_seq = 0
    try:
        while True:
            for seq, line in tracker.get_log(since_seq=last_seq):
                print(line)
                last_seq = seq
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
