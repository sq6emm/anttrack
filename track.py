#!/usr/bin/env python3

import configparser
import csv
import os
import sys
import time

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

    while True:
        try:
            t = ts.now()
            if isinstance(target, EarthSatellite):
                diff = target - qth
                astrometric = diff.at(t)
                alt, az_angle, _ = astrometric.altaz()
            else:
                astrometric = qth.at(t).observe(target)
                alt, az_angle, _ = astrometric.apparent().altaz()

            el = alt.degrees
            az = az_angle.degrees
            if el > 0:
                print("SET", t.utc_strftime(), round(az, 1), round(el, 1))
                rot.set_position(round(az, 1), round(el, 1))
                time.sleep(1)
            else:
                print("BELOW HORIZON", t.utc_strftime(), round(az, 2), round(el, 2))
            raz, rel = rot.get_position()
            print("GET", t.utc_strftime(), round(raz, 2), round(rel, 2))
            time.sleep(3)
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
            time.sleep(3)


if __name__ == "__main__":
    main()
