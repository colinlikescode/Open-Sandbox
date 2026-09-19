"""Parsing and formatting of human-friendly sizes, durations and CPU counts."""

from __future__ import annotations

import re

from opensandbox.errors import ValidationError

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ti": 1024**4,
    "tib": 1024**4,
}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?", re.IGNORECASE)
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}


def parse_bytes(value: str | int | float) -> int:
    """Parse ``"4GB"``, ``"512Mi"``, ``4096`` into bytes."""
    if isinstance(value, bool):
        raise ValidationError(f"Invalid size: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValidationError(f"Size must be non-negative: {value!r}")
        return int(value)
    match = _SIZE_RE.match(value)
    if not match:
        raise ValidationError(f"Invalid size: {value!r}. Use forms like '4GB', '512MB', '1Gi'.")
    number, unit = match.groups()
    mult = _UNITS.get(unit.lower())
    if mult is None:
        raise ValidationError(f"Unknown size unit {unit!r} in {value!r}")
    return int(float(number) * mult)


def format_bytes(num: int | float) -> str:
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1000
    return f"{n:.1f} TB"


def parse_duration(value: str | int | float) -> float:
    """Parse ``"15m"``, ``"1h30m"``, ``"900"``, ``900`` into seconds."""
    if isinstance(value, bool):
        raise ValidationError(f"Invalid duration: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValidationError(f"Duration must be non-negative: {value!r}")
        return float(value)
    text = value.strip()
    if not text:
        raise ValidationError("Duration must not be empty")
    pos = 0
    total = 0.0
    while pos < len(text):
        match = _DURATION_RE.match(text, pos)
        if not match or match.end() == pos:
            raise ValidationError(
                f"Invalid duration: {value!r}. Use forms like '900', '15m', '1h30m'."
            )
        number, unit = match.groups()
        total += float(number) * _DURATION_UNITS[unit.lower() if unit else None]
        pos = match.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return total


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m" if not s else f"{m}m{s}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h" if not m else f"{h}h{m}m"


def cpus_to_millis(cpus: float | int | str) -> int:
    """Normalize ``0.5`` -> ``500`` millicpu; validate positive."""
    try:
        value = float(cpus)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid cpus value: {cpus!r}") from exc
    if value <= 0:
        raise ValidationError(f"cpus must be positive, got {cpus!r}")
    millis = round(value * 1000)
    if millis <= 0:
        raise ValidationError(f"cpus too small: {cpus!r}")
    return millis


def millis_to_cpus(millis: int) -> float:
    return millis / 1000.0
