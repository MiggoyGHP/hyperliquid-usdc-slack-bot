"""Severity bands with hysteresis.

A reading hovering around 80.00% would otherwise flip band on every tick and
post an alert each time. Entry thresholds are the real numbers that matter;
leaving a band requires dropping a further EXIT_BUFFER below its entry.
"""

from decimal import Decimal
from enum import IntEnum

EXIT_BUFFER = Decimal("0.015")


class Band(IntEnum):
    """Ordered by severity, so upgrades and downgrades are plain comparisons."""

    NORMAL = 0
    HIGH = 1
    CRITICAL = 2


ENTRY = {
    Band.HIGH: Decimal("0.80"),
    Band.CRITICAL: Decimal("0.90"),
}


def _raw_band(utilization: Decimal) -> Band:
    """Band from entry thresholds alone, ignoring where we came from."""
    if utilization >= ENTRY[Band.CRITICAL]:
        return Band.CRITICAL
    if utilization >= ENTRY[Band.HIGH]:
        return Band.HIGH
    return Band.NORMAL


def classify(utilization: Decimal, prev: Band | None) -> Band:
    """Band for this reading, holding the previous band inside the buffer."""
    raw = _raw_band(utilization)

    if prev is None or raw >= prev:
        return raw

    # Downgrade: only allowed once we clear the previous band's exit level.
    if utilization >= ENTRY[prev] - EXIT_BUFFER:
        return prev

    return raw
