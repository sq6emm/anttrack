import configparser
import os
import sys

CONFIG_FILE = os.environ.get(
    "ANTTRACK_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini"),
)

# Where satellite/ephemeris caches are kept. Defaults alongside the config
# file so a single mounted volume (e.g. in Docker) covers both.
DATA_DIR = os.environ.get("ANTTRACK_DATA_DIR", os.path.dirname(CONFIG_FILE))

# Optional shared-secret API key. When set, the API requires it on every
# request via the X-API-Key header. Unset by default for LAN/dev use.
API_KEY = os.environ.get("ANTTRACK_API_KEY")


def load_config(config_file=CONFIG_FILE):
    """Load QTH location and rotator settings from config_file."""
    if not os.path.exists(config_file):
        sys.exit(
            f"ERROR: config file not found: {config_file}\n"
            "Copy config.ini.example to config.ini and fill in your own "
            "QTH and rotator settings."
        )

    parser = configparser.ConfigParser()
    parser.read(config_file)
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
        sys.exit(f"ERROR: invalid config file {config_file}: {exc}")

    return qth_cfg, rotator_cfg
