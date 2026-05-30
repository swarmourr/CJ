"""Duration string parsing utility."""

from __future__ import annotations
import re


def parse_duration(value: str | int | float) -> float:
    """Parse a human-readable duration string into seconds.

    Parameters
    ----------
    value : str or int or float
        Duration as a number (seconds) or string like ``"10m"``,
        ``"1h"``, ``"30s"``, ``"1h30m"``, ``"90s"``.

    Returns
    -------
    float
        Total duration in seconds.

    Raises
    ------
    ValueError
        If the string cannot be parsed.

    Examples
    --------
    >>> parse_duration("10m")
    600.0
    >>> parse_duration("1h30m")
    5400.0
    >>> parse_duration("30s")
    30.0
    >>> parse_duration(120)
    120.0
    """
    if isinstance(value, (int, float)):
        return float(value)

    value = value.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    pattern = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)", re.IGNORECASE)
    matches = pattern.findall(value)

    if not matches or not any(m[0] for m in matches):
        raise ValueError(f"Cannot parse duration: {value!r}")

    total = 0.0
    for amount, unit in matches:
        if not amount:
            continue
        unit = unit.lower() or "s"
        total += float(amount) * units[unit]

    return total
