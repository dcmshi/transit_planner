"""
Shared GTFS time helpers.

GTFS times are HH:MM:SS strings that may exceed 24:00:00 for trips crossing
midnight (e.g. "25:30:00" = 1:30 AM on the next service day).  Stored as
strings in the DB; converted to integer seconds past midnight when needed.
"""

import logging

logger = logging.getLogger(__name__)


def hms_to_seconds(hms: str) -> int:
    """
    Convert HH:MM:SS (possibly HH > 23) to integer seconds past midnight.
    Returns 0 (and logs a warning) on parse failure.
    """
    try:
        parts = hms.strip().split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError, AttributeError):
        logger.warning("hms_to_seconds: could not parse %r, defaulting to 0", hms)
        return 0
