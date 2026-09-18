"""Background rotator-tracking engine.

Refactors the original single-shot CLI loop into a controllable service:
one target can be tracked (or pointed) at a time, running in a background
thread, while the current state is readable at any moment from other
threads (e.g. API request handlers).
"""

import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import Hamlib
from pyhamtools.locator import calculate_distance, calculate_heading, latlong_to_locator
from skyfield.api import E, N, wgs84

from .geo import azimuth_diff, next_stall_count
from .satellites import (
    SATELLITE_UPDATE_INTERVAL_S,
    SLOW_TARGET_UPDATE_INTERVAL_S,
    body_names,
    load_planets,
    load_satellites,
    loader,
)

Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_NONE)

# Consecutive non-improving over-threshold checks before a pointing error
# is treated as a real stall/fault rather than the dish still slewing.
ROTATOR_STALL_CHECKS = 3
# Ignore read-back changes smaller than this as slew progress (float/encoder jitter).
ROTATOR_IMPROVE_MARGIN_DEG = 0.05
# Skip re-commanding the rotator when the new position is this close to the
# last one actually sent, to avoid pointless traffic/wear between checks.
ROTATOR_DEADBAND_DEG = 0.05

# Seconds to wait before retrying after a rotator communication error.
RECONNECT_DELAY_S = 3.0

LOG_MAXLEN = 500


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TrackerError(Exception):
    """Raised for user-facing errors (unknown target, bad request, ...)."""


class Tracker:
    def __init__(self, qth_cfg, rotator_cfg):
        self.qth_cfg = qth_cfg
        self.rotator_cfg = rotator_cfg
        self.qth_wwl = latlong_to_locator(qth_cfg["lat"], qth_cfg["lon"], 10)
        self._ts = loader.timescale()

        self._lock = threading.RLock()
        self._rot = None
        self._thread = None
        self._stop_event = threading.Event()

        self._planets = None
        self._satellites_by_name = None

        self._log_seq = 0
        self._log = deque(maxlen=LOG_MAXLEN)

        self._state = {
            "mode": "idle",              # idle | satellite | planet | loc | raw
            "request": None,
            "started_utc": None,
            "last_update_utc": None,
            "commanded": None,           # {"az":..,"el":..}
            "actual": None,              # {"az":..,"el":..}
            "below_horizon": None,
            "connected": False,
            "warning": None,
            "error": None,
            "az_error_deg": None,
            "el_error_deg": None,
            "heading_deg": None,         # great-circle heading, loc mode only
            "distance_km": None,         # great-circle distance, loc mode only
            "lead_time_s": rotator_cfg["lead_time_s"],
            "error_threshold_deg": rotator_cfg["error_threshold_deg"],
        }

        self.refresh_catalog()

    # -- catalog -----------------------------------------------------

    def refresh_catalog(self):
        """(Re)load the satellite catalog and planetary ephemeris."""
        self._planets = load_planets()
        by_name, warning = load_satellites()
        with self._lock:
            self._satellites_by_name = by_name
            if warning:
                self._log_line(f"WARNING: {warning}")
        return {"satellites": len(by_name), "bodies": len(body_names(self._planets))}

    def catalog(self, query=None):
        with self._lock:
            sat_names = sorted(self._satellites_by_name.keys())
        bodies = body_names(self._planets)
        if query:
            q = query.strip().upper()
            sat_names = [n for n in sat_names if q in n.upper()]
            bodies = [n for n in bodies if q in n.upper()]
        return {"satellites": sat_names, "bodies": bodies}

    # -- status / log --------------------------------------------------

    def status(self):
        with self._lock:
            return dict(self._state)

    def get_log(self, since_seq=0, limit=200):
        with self._lock:
            lines = [(seq, line) for seq, line in self._log if seq > since_seq]
        return lines[-limit:]

    def _log_line(self, msg):
        # Caller must hold self._lock.
        self._log_seq += 1
        self._log.append((self._log_seq, f"{_utcnow_iso()} {msg}"))

    # -- rotator lifecycle ----------------------------------------------

    def _ensure_rotator(self):
        if self._rot is None:
            rot = Hamlib.Rot(rot_model=self.rotator_cfg["model"])
            rot.set_conf("rot_pathname", self.rotator_cfg["host"])
            self._rot = rot
        return self._rot

    def close(self):
        """Stop any active tracking and release the rotator connection."""
        self.stop()
        if self._rot is not None:
            try:
                self._rot.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._rot = None

    # -- target selection -------------------------------------------------

    def _qth_topocentric(self):
        return wgs84.latlon(
            self.qth_cfg["lat"] * N, self.qth_cfg["lon"] * E,
            elevation_m=self.qth_cfg["ele"],
        )

    def _aim_fn_for_target(self, target, is_satellite):
        if is_satellite:
            qth = self._qth_topocentric()

            def aim_fn(t):
                diff = target - qth
                alt, az_angle, _ = diff.at(t).altaz()
                return az_angle.degrees, alt.degrees
        else:
            qth = self._planets['earth'] + self._qth_topocentric()

            def aim_fn(t):
                astrometric = qth.at(t).observe(target).apparent()
                alt, az_angle, _ = astrometric.altaz()
                return az_angle.degrees, alt.degrees
        return aim_fn

    def start_target(self, name):
        """Start continuously tracking a named satellite or solar-system body."""
        name = name.strip()
        key = name.upper()
        with self._lock:
            satellites_by_name = self._satellites_by_name

        try:
            target = self._planets[key]
            is_satellite = False
        except (KeyError, ValueError):
            target = satellites_by_name.get(key)
            if target is None:
                # Names in the catalog aren't necessarily upper-cased ("ISS (ZARYA)").
                target = next((sat for sat_name, sat in satellites_by_name.items()
                               if sat_name.upper() == key), None)
            if target is None:
                raise TrackerError(f"No object named {name!r} found")
            is_satellite = True

        aim_fn = self._aim_fn_for_target(target, is_satellite)
        mode = "satellite" if is_satellite else "planet"
        update_interval_s = (SATELLITE_UPDATE_INTERVAL_S if is_satellite
                              else SLOW_TARGET_UPDATE_INTERVAL_S)
        self._start_loop(mode, name, aim_fn, update_interval_s, check_horizon=True)

    def start_loc(self, locator):
        """Point (and hold) at the heading toward a Maidenhead locator."""
        locator = locator.strip()
        try:
            heading = round(calculate_heading(self.qth_wwl, locator), 1)
            distance = round(calculate_distance(self.qth_wwl, locator), 1)
        except Exception as exc:
            raise TrackerError(f"invalid locator {locator!r}: {exc}") from exc

        with self._lock:
            self._state["heading_deg"] = heading
            self._state["distance_km"] = distance

        aim_fn = lambda t: (heading, 0.0)  # noqa: E731
        self._start_loop("loc", locator.upper(), aim_fn, SLOW_TARGET_UPDATE_INTERVAL_S,
                          check_horizon=False)

    def start_raw(self, az, el):
        """Point (and hold) at a raw azimuth/elevation."""
        with self._lock:
            self._state["heading_deg"] = None
            self._state["distance_km"] = None
        aim_fn = lambda t: (az, el)  # noqa: E731
        self._start_loop("raw", f"az={az} el={el}", aim_fn, SLOW_TARGET_UPDATE_INTERVAL_S,
                          check_horizon=False)

    def stop(self):
        """Stop any active tracking/pointing loop and idle the rotator link."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        self._thread = None
        with self._lock:
            self._state.update(mode="idle", request=None, commanded=None,
                                connected=False, below_horizon=None,
                                az_error_deg=None, el_error_deg=None)
            self._log_line("STOP")

    # -- the loop ------------------------------------------------------

    def _start_loop(self, mode, request_label, aim_fn, update_interval_s, check_horizon):
        self.stop()
        self._stop_event = threading.Event()
        with self._lock:
            self._state.update(mode=mode, request=request_label,
                                started_utc=_utcnow_iso(), warning=None, error=None)
            self._log_line(f"START {mode} {request_label}")
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(mode, aim_fn, update_interval_s, check_horizon, self._stop_event),
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self, mode, aim_fn, update_interval_s, check_horizon, stop_event):
        rot = self._ensure_rotator()
        try:
            rot.open()
        except Exception as exc:  # pylint: disable=broad-except
            with self._lock:
                self._state.update(error=f"failed to open rotator: {exc}", connected=False)
                self._log_line(f"ERROR: failed to open rotator ({exc})")
            return

        lead_time_s = self.rotator_cfg["lead_time_s"]
        error_threshold_deg = self.rotator_cfg["error_threshold_deg"]
        last_cmd_az, last_cmd_el = None, None
        prev_az_error, prev_el_error = None, None
        az_stall_count, el_stall_count = 0, 0

        with self._lock:
            self._state.update(connected=True)

        while not stop_event.is_set():
            try:
                t = self._ts.now()
                t_lead = t + timedelta(seconds=lead_time_s) if lead_time_s else t
                az, el = aim_fn(t_lead)
                az, el = float(az), float(el)

                below_horizon = bool(check_horizon and el <= 0)
                if not below_horizon:
                    cmd_az, cmd_el = round(az, 1), round(el, 1)
                    moved = (last_cmd_az is None
                             or azimuth_diff(cmd_az, last_cmd_az) >= ROTATOR_DEADBAND_DEG
                             or abs(cmd_el - last_cmd_el) >= ROTATOR_DEADBAND_DEG)
                    if moved:
                        rot.set_position(cmd_az, cmd_el)
                    else:
                        cmd_az, cmd_el = last_cmd_az, last_cmd_el
                else:
                    cmd_az, cmd_el = None, None

                raz, rel = rot.get_position()

                az_error = el_error = None
                warning = None
                if last_cmd_az is not None:
                    az_error = azimuth_diff(raz, last_cmd_az)
                    el_error = abs(rel - last_cmd_el)

                    az_stall_count = next_stall_count(
                        az_error, prev_az_error, az_stall_count,
                        error_threshold_deg, ROTATOR_IMPROVE_MARGIN_DEG)
                    el_stall_count = next_stall_count(
                        el_error, prev_el_error, el_stall_count,
                        error_threshold_deg, ROTATOR_IMPROVE_MARGIN_DEG)
                    prev_az_error, prev_el_error = az_error, el_error

                    if (az_stall_count >= ROTATOR_STALL_CHECKS
                            or el_stall_count >= ROTATOR_STALL_CHECKS):
                        warning = (
                            f"rotator not converging on commanded position "
                            f"(commanded {last_cmd_az},{last_cmd_el}, actual {raz},{rel}, "
                            f"az_error {az_error:.2f}deg, el_error {el_error:.2f}deg); "
                            "check for backlash, a stall, or a mechanical fault"
                        )
                else:
                    prev_az_error, prev_el_error = None, None
                    az_stall_count, el_stall_count = 0, 0

                with self._lock:
                    self._state.update(
                        last_update_utc=_utcnow_iso(),
                        commanded=({"az": cmd_az, "el": cmd_el} if cmd_az is not None else None),
                        actual={"az": round(raz, 2), "el": round(rel, 2)},
                        below_horizon=below_horizon,
                        connected=True,
                        error=None,
                        warning=warning,
                        az_error_deg=(round(az_error, 2) if az_error is not None else None),
                        el_error_deg=(round(el_error, 2) if el_error is not None else None),
                    )
                    if below_horizon:
                        self._log_line(f"BELOW HORIZON az={round(az, 2)} el={round(el, 2)}")
                    elif cmd_az is not None and (last_cmd_az, last_cmd_el) != (cmd_az, cmd_el):
                        self._log_line(f"SET az={cmd_az} el={cmd_el}")
                    self._log_line(f"GET az={round(raz, 2)} el={round(rel, 2)}")
                    if warning:
                        self._log_line(f"WARNING: {warning}")

                last_cmd_az, last_cmd_el = cmd_az, cmd_el
                stop_event.wait(update_interval_s)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # pylint: disable=broad-except
                # Reconnect on any rotator/communication error (e.g. the
                # rot_pathname link dropping) instead of crashing the tracker.
                with self._lock:
                    self._state.update(connected=False, error=str(exc))
                    self._log_line(f"WARNING: rotator error ({exc}); reconnecting...")
                try:
                    rot.close()
                except Exception:  # pylint: disable=broad-except
                    pass
                try:
                    rot.open()
                except Exception as reopen_exc:  # pylint: disable=broad-except
                    with self._lock:
                        self._log_line(
                            f"ERROR: failed to reopen rotator connection "
                            f"({reopen_exc}); retrying in {RECONNECT_DELAY_S:g}s")
                # Don't compare against a position commanded before the outage:
                # a large gap right after reconnecting is expected, not a fault.
                last_cmd_az, last_cmd_el = None, None
                prev_az_error, prev_el_error = None, None
                az_stall_count, el_stall_count = 0, 0
                stop_event.wait(RECONNECT_DELAY_S)

        try:
            rot.close()
        except Exception:  # pylint: disable=broad-except
            pass
