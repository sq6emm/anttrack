"""Small geometry/control helpers shared by the tracking loop."""


def azimuth_diff(a, b):
    """Angular difference between two azimuths, handling 0/360 wraparound."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def next_stall_count(error, prev_error, stall_count, threshold, margin):
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
