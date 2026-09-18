"""Satellite catalog and planetary ephemeris loading."""

import csv
import os

from skyfield.api import EarthSatellite, Loader

from .config import DATA_DIR

SATELLITE_CACHE_FILE = 'satellites.csv'  # custom filename, not 'gp.php'
SATELLITE_CACHE_MAX_DAYS = 3.0           # re-download once cache is this old
SATELLITE_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=csv'

# Update cadence for fast-moving targets (satellites can cross the sky in
# minutes, with high angular velocity near zenith).
SATELLITE_UPDATE_INTERVAL_S = 3.0
# Update cadence for targets whose apparent motion is dominated by Earth's
# rotation (~15 deg/hour): sun, moon, planets, stars. At that rate this
# interval keeps drift well under the dish's beamwidth between checks.
SLOW_TARGET_UPDATE_INTERVAL_S = 30.0

# Bodies available from de421.bsp, used as a fallback if the kernel's own
# name index can't be introspected.
FALLBACK_BODY_NAMES = [
    "SUN", "MOON", "MERCURY BARYCENTER", "VENUS BARYCENTER", "MARS BARYCENTER",
    "JUPITER BARYCENTER", "SATURN BARYCENTER", "URANUS BARYCENTER",
    "NEPTUNE BARYCENTER", "PLUTO BARYCENTER",
]

os.makedirs(DATA_DIR, exist_ok=True)
loader = Loader(DATA_DIR)


class CatalogError(Exception):
    pass


def load_satellites():
    """Load the amateur-satellite catalog.

    Downloads a fresh copy when the local cache is missing or older than
    SATELLITE_CACHE_MAX_DAYS. If the download fails (e.g. no internet
    access), falls back to the existing cached copy when there is one.

    Returns (by_name, warning) where warning is a message string if a
    stale cache had to be used, otherwise None.
    """
    ts = loader.timescale()
    warning = None
    is_stale = (not loader.exists(SATELLITE_CACHE_FILE)
                or loader.days_old(SATELLITE_CACHE_FILE) >= SATELLITE_CACHE_MAX_DAYS)

    if is_stale:
        try:
            loader.download(SATELLITE_URL, filename=SATELLITE_CACHE_FILE)
        except OSError as exc:
            if loader.exists(SATELLITE_CACHE_FILE):
                warning = f"satellite data download failed ({exc}); using cached copy"
            else:
                raise CatalogError(
                    f"satellite data download failed ({exc}) and no cached "
                    f"{SATELLITE_CACHE_FILE} is available"
                ) from exc

    with loader.open(SATELLITE_CACHE_FILE, mode='r') as f:
        data = list(csv.DictReader(f))

    by_name = {sat.name: sat for sat in
               (EarthSatellite.from_omm(ts, fields) for fields in data)}
    return by_name, warning


def load_planets():
    """Load the JPL ephemeris.

    Skyfield only downloads de421.bsp the first time; once it is cached on
    disk it is reused with no network access.
    """
    try:
        return loader('de421.bsp')
    except OSError as exc:
        raise CatalogError(f"could not download or open de421.bsp ({exc})") from exc


def body_names(planets):
    try:
        names = sorted({n for names in planets.names().values() for n in names
                         if n.replace(' ', '').isalpha()})
        if names:
            return names
    except Exception:  # pylint: disable=broad-except
        pass
    return list(FALLBACK_BODY_NAMES)
