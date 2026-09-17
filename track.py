#!/usr/bin/env python3

import configparser
import csv
import os
import sys
import time
from datetime import timedelta

from skyfield.api import wgs84, N, E, load, EarthSatellite
from pyhamtools.locator import calculate_distance, calculate_heading, latlong_to_locator

import Hamlib

Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_NONE)

CONFIG_FILE = os.environ.get(
    "ANTTRACK_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"),
)

SATELLITE_CACHE_FILE = 'satellites.csv'  # custom filename, not 'gp.php'
SATELLITE_CACHE_MAX_DAYS = 3.0           # re-download once cache is this old
SATELLITE_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=csv'

# Consecutive non-improving over-threshold checks before a pointing error
# is treated as a real stall/fault rather than the dish still slewing.
ROTATOR_STALL_CHECKS = 3
# Ignore read-back changes smaller than this as slew progress (float/encoder jitter).
ROTATOR_IMPROVE_MARGIN_DEG = 0.05
# Skip re-commanding the rotator when the new position is this close to the
# last one actually sent, to avoid pointless traffic/wear between checks.
ROTATOR_DEADBAND_DEG = 0.05

# Update cadence for fast-moving targets (satellites can cross the sky in
# minutes, with high angular velocity near zenith).
SATELLITE_UPDATE_INTERVAL_S = 3.0
# Update cadence for targets whose apparent motion is dominated by Earth's
# rotation (~15 deg/hour): sun, moon, planets, stars. At that rate this
# interval keeps drift well under the dish's beamwidth between checks.
SLOW_TARGET_UPDATE_INTERVAL_S = 30.0


def load_config():
    """Load QTH location and rotator settings from CONFIG_FILE."""
    if not os.path.exists(CONFIG_FILE):
        sys.exit(
            f"ERROR: config file not found: {CONFIG_FILE}\n"
            "Copy config.ini.example to config.ini and fill in your own "
            "QTH and rotator settings."
        )

    parser = configparser.ConfigParser()
    parser.read(CONFIG_FILE)
    try:
        qth_cfg = {
            "lat": parser.getfloat("qth", "latitude"),
            "lon": parser.getfloat("qth", "longitude"),
            "ele": parser.getfloat("qth", "elevation_m"),
        }
        rotator_cfg = {
            "model": parser.getint("rotator", "model"),
            "host": parser.get("rotator", "host"),
            # Seconds to command the rotator ahead of real time, to
            # compensate for its own slew lag. 0 disables lead compensation.
            "lead_time_s": parser.getfloat("rotator", "lead_time_s", fallback=0.0),
            # Max allowed difference (degrees) between a previously
            # commanded position and the rotator's actual read-back
            # before it's flagged as a possible stall/backlash/fault.
            "error_threshold_deg": parser.getfloat(
                "rotator", "error_threshold_deg", fallback=0.3
            ),
        }
    except (configparser.Error, ValueError) as exc:
        sys.exit(f"ERROR: invalid config file {CONFIG_FILE}: {exc}")

    return qth_cfg, rotator_cfg


def make_rotator(rotator_cfg):
    rot = Hamlib.Rot(rot_model=rotator_cfg["model"])
    rot.set_conf("rot_pathname", rotator_cfg["host"])
    return rot


def load_satellites(loader, ts):
    """Load the amateur-satellite catalog.

    Downloads a fresh copy when the local cache is missing or older than
    SATELLITE_CACHE_MAX_DAYS. If the download fails (e.g. no internet
    access), falls back to the existing cached copy when there is one.
    """
    is_stale = (not loader.exists(SATELLITE_CACHE_FILE)
                or loader.days_old(SATELLITE_CACHE_FILE) >= SATELLITE_CACHE_MAX_DAYS)

    if is_stale:
        try:
            loader.download(SATELLITE_URL, filename=SATELLITE_CACHE_FILE)
        except OSError as exc:
            if loader.exists(SATELLITE_CACHE_FILE):
                print(f"WARNING: satellite data download failed ({exc}); "
                      f"using cached {SATELLITE_CACHE_FILE}")
            else:
                print(f"ERROR: satellite data download failed ({exc}) and "
                      f"no cached {SATELLITE_CACHE_FILE} is available")
                sys.exit(1)

    with loader.open(SATELLITE_CACHE_FILE, mode='r') as f:
        data = list(csv.DictReader(f))

    return {sat.name: sat for sat in
            (EarthSatellite.from_omm(ts, fields) for fields in data)}


def _azimuth_diff(a, b):
    """Angular difference between two azimuths, handling 0/360 wraparound."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def _next_stall_count(error, prev_error, stall_count, threshold, margin):
    """Track whether a rotator axis is stuck rather than still slewing.

    Returns 0 whenever the error is within tolerance or has meaningfully
    shrunk since the last check (still converging on the commanded
    position). Otherwise increments the count, so a real stall/backlash/
    fault only gets flagged once the error fails to improve over several
    consecutive checks, not on any single reading taken mid-slew.
    """
    if error <= threshold:
        return 0
    if prev_error is not None and error <= prev_error - margin:
        return 0
    return stall_count + 1


def load_planets(loader):
    """Load the JPL ephemeris.

    Skyfield only downloads de421.bsp the first time; once it is cached on
    disk it is reused with no network access. Any download/open failure is
    reported cleanly instead of raising a raw traceback.
    """
    try:
        return loader('de421.bsp')
    except OSError as exc:
        print(f"ERROR: could not download or open de421.bsp ({exc})")
        sys.exit(1)


def main():
    qth_cfg, rotator_cfg = load_config()
    rot = make_rotator(rotator_cfg)
    qth_wwl = latlong_to_locator(qth_cfg["lat"], qth_cfg["lon"], 10)

    ts = load.timescale()
    t = ts.now()

    if len(sys.argv) == 3 and sys.argv[1].lower() == "loc":
        rem_head = round(calculate_heading(qth_wwl, sys.argv[2]), 2)
        rem_dist = round(calculate_distance(qth_wwl, sys.argv[2]), 2)
        print("SET", t.utc_strftime(), round(rem_head, 1), 0, rem_dist, "km")
        rot.open()
        rot.set_position(round(rem_head, 1), 0)
        rot.close()
        sys.exit()
    elif len(sys.argv) in (3, 4) and sys.argv[1].lower() == "raw":
        raw_az = float(sys.argv[2])
        raw_el = float(sys.argv[3])
        print("SET", t.utc_strftime(), raw_az, raw_el)
        rot.open()
        rot.set_position(raw_az, raw_el)
        rot.close()
        sys.exit()
    elif len(sys.argv) == 2:
        request = sys.argv[1].upper()
    else:
        print("No object name given, or")
        print("No loc and locator given")
        print("Exiting...")
        sys.exit(1)

    planets = load_planets(load)
    by_name = load_satellites(load, ts)

    qth = wgs84.latlon(qth_cfg["lat"] * N, qth_cfg["lon"] * E, elevation_m=qth_cfg["ele"])

    try:
        target = planets[request]
        qth = planets['earth'] + qth
    except KeyError:
        target = by_name.get(request)
        if target is None:
            print("No object found")
            print("Exiting...")
            sys.exit(1)

    rot.open()

    lead_time_s = rotator_cfg["lead_time_s"]
    error_threshold_deg = rotator_cfg["error_threshold_deg"]
    last_cmd_az, last_cmd_el = None, None
    prev_az_error, prev_el_error = None, None
    az_stall_count, el_stall_count = 0, 0

    is_satellite = isinstance(target, EarthSatellite)
    update_interval_s = (SATELLITE_UPDATE_INTERVAL_S if is_satellite
                          else SLOW_TARGET_UPDATE_INTERVAL_S)

    while True:
        try:
            t = ts.now()
            t_lead = t + timedelta(seconds=lead_time_s) if lead_time_s else t
            if is_satellite:
                diff = target - qth
                astrometric = diff.at(t_lead)
                alt, az_angle, _ = astrometric.altaz()
            else:
                astrometric = qth.at(t_lead).observe(target)
                alt, az_angle, _ = astrometric.apparent().altaz()

            el = alt.degrees
            az = az_angle.degrees
            if el > 0:
                cmd_az, cmd_el = round(az, 1), round(el, 1)
                lead_note = f" (lead {lead_time_s:g}s)" if lead_time_s else ""
                moved = (last_cmd_az is None
                         or _azimuth_diff(cmd_az, last_cmd_az) >= ROTATOR_DEADBAND_DEG
                         or abs(cmd_el - last_cmd_el) >= ROTATOR_DEADBAND_DEG)
                if moved:
                    print("SET", t.utc_strftime(), cmd_az, cmd_el, lead_note)
                    rot.set_position(cmd_az, cmd_el)
                else:
                    print("SET", t.utc_strftime(), cmd_az, cmd_el, lead_note,
                          "(unchanged, skipped)")
                    cmd_az, cmd_el = last_cmd_az, last_cmd_el
            else:
                cmd_az, cmd_el = None, None
                print("BELOW HORIZON", t.utc_strftime(), round(az, 2), round(el, 2))

            raz, rel = rot.get_position()
            print("GET", t.utc_strftime(), round(raz, 2), round(rel, 2))

            if last_cmd_az is not None:
                az_error = _azimuth_diff(raz, last_cmd_az)
                el_error = abs(rel - last_cmd_el)

                az_stall_count = _next_stall_count(
                    az_error, prev_az_error, az_stall_count,
                    error_threshold_deg, ROTATOR_IMPROVE_MARGIN_DEG)
                el_stall_count = _next_stall_count(
                    el_error, prev_el_error, el_stall_count,
                    error_threshold_deg, ROTATOR_IMPROVE_MARGIN_DEG)
                prev_az_error, prev_el_error = az_error, el_error

                if (az_stall_count >= ROTATOR_STALL_CHECKS
                        or el_stall_count >= ROTATOR_STALL_CHECKS):
                    print(f"WARNING: rotator not converging on commanded "
                          f"position (commanded {last_cmd_az},{last_cmd_el}, "
                          f"actual {raz},{rel}, az_error {az_error:.2f}deg, "
                          f"el_error {el_error:.2f}deg); check for backlash, "
                          "a stall, or a mechanical fault")
            else:
                prev_az_error, prev_el_error = None, None
                az_stall_count, el_stall_count = 0, 0

            last_cmd_az, last_cmd_el = cmd_az, cmd_el
            time.sleep(update_interval_s)
        except (KeyboardInterrupt, SystemExit):
            print("Exiting...")
            try:
                rot.close()
            except Exception:  # pylint: disable=broad-except
                pass
            sys.exit()
        except Exception as exc:  # pylint: disable=broad-except
            # Reconnect on any rotator/communication error (e.g. the
            # rot_pathname link dropping) instead of crashing the tracker.
            print(f"WARNING: rotator error ({exc}); reconnecting...")
            try:
                rot.close()
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                rot.open()
            except Exception as reopen_exc:  # pylint: disable=broad-except
                print(f"ERROR: failed to reopen rotator connection ({reopen_exc}); retrying in 3s")
            # Don't compare against a position commanded before the outage:
            # a large gap right after reconnecting is expected, not a fault.
            last_cmd_az, last_cmd_el = None, None
            prev_az_error, prev_el_error = None, None
            az_stall_count, el_stall_count = 0, 0
            time.sleep(3)


if __name__ == "__main__":
    main()
