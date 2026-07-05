"""Flight number generation for Wings of Canada (ICAO: WOC / IATA: CW).

Numbering rules
---------------
Every leg gets its own 4-digit number N. The flight number is "CW" + N and the
radio callsign is "WOC" + N (the same number is used for both).

The first digit of N is the hub the leg touches. When both endpoints are hubs
the DEPARTURE hub wins. Toronto's digit depends on whether the leg is domestic:

    1 - Vancouver  (CYVR)
    2 - Calgary    (CYYC)
    3 - Edmonton   (CYEG)
    4 - Toronto    (CYYZ) on an international leg
    5 - Toronto    (CYYZ) on a domestic leg
    6 - Montreal   (CYUL)
    7 - any other Canadian airport (no hub touched)
    8 - a US airport (no Canadian airport involved)
    9 - everything else (no Canadian or US airport involved)

Each leg is numbered independently (the outbound and return are NOT a coupled
pair). A round-trip is two separate allocations, so e.g. CYYZ->CYVR is a Toronto
domestic number (5xxx) while its return CYVR->CYYZ is a Vancouver number (1xxx).

Parity ("flights departing from Canada must use an even number, including 0"):

    leg departs Canada -> even number
    leg departs abroad -> odd number

Within a series a free number of the right parity is chosen AT RANDOM (rather
than always the lowest), so route numbers are spread across the series instead
of clustering at x000. Keeping Canada-departing legs even and abroad-departing
legs odd means a leg and its reverse can never collide.
"""
import random as _random

# Single-digit hubs. Toronto is handled separately (4 international / 5 domestic).
HUB_DIGITS = {
    "CYVR": 1,
    "CYYC": 2,
    "CYEG": 3,
    "CYUL": 6,
}
TORONTO = "CYYZ"

# ICAO prefixes covering the USA and its territories.
US_PREFIXES = ("K", "PA", "PF", "PO", "PP", "PH", "PG", "PJ", "PM", "PW", "TJ", "TI")

# Local Training flights are filed ad-hoc by pilots: a same-airport local
# session with any aircraft and any number in this reserved block. Scheduled
# "rest of world" (9xxx) numbers stop at 9899 so the two never collide.
TRAINING_MIN = 9900
TRAINING_MAX = 9999


class SeriesFullError(Exception):
    """Raised when every number in the relevant series is already taken."""


def is_canadian(icao: str) -> bool:
    return icao.upper().startswith("C")


def is_us(icao: str) -> bool:
    icao = icao.upper()
    return any(icao.startswith(p) for p in US_PREFIXES)


def _hub_digit(icao: str, domestic: bool):
    """The series digit for an airport if it is a hub, else None."""
    icao = icao.upper()
    if icao in HUB_DIGITS:
        return HUB_DIGITS[icao]
    if icao == TORONTO:
        return 5 if domestic else 4
    return None


def classify(dep: str, arr: str) -> int:
    """Return the leading digit for a leg from dep to arr.

    The departure hub wins when both endpoints are hubs.
    """
    dep, arr = dep.upper(), arr.upper()
    domestic = is_canadian(dep) and is_canadian(arr)

    dep_hub = _hub_digit(dep, domestic)
    if dep_hub is not None:
        return dep_hub
    arr_hub = _hub_digit(arr, domestic)
    if arr_hub is not None:
        return arr_hub

    if is_canadian(dep) or is_canadian(arr):
        return 7
    if is_us(dep) or is_us(arr):
        return 8
    return 9


def series_range(digit: int) -> range:
    """The block of numbers owned by a series.

    The 9xxx "rest of world" series stops at 9899 because 9900-9999 is reserved
    for pilot-filed Local Training flights.
    """
    if digit == 9:
        return range(9000, TRAINING_MIN)
    return range(digit * 1000, digit * 1000 + 1000)


def is_training_number(n: int) -> bool:
    """True if n falls in the reserved Local Training block (9900-9999)."""
    return TRAINING_MIN <= n <= TRAINING_MAX


def allocate_one(dep: str, arr: str, used: set[int], rng=_random) -> int:
    """Pick a random free number for a single leg from dep to arr.

    `used` is the set of route numbers already assigned. Legs departing Canada
    are even; legs departing abroad are odd. A free number of the right parity
    is chosen at random within the series. `rng` (anything with `.choice`) can
    be supplied for deterministic tests.
    """
    dep, arr = dep.upper(), arr.upper()
    digit = classify(dep, arr)
    parity = 0 if is_canadian(dep) else 1

    candidates = [
        n for n in series_range(digit) if n % 2 == parity and n not in used
    ]
    if not candidates:
        raise SeriesFullError(
            f"No free flight numbers left in the {digit}xxx series for {dep}-{arr}."
        )
    return rng.choice(candidates)


def flight_no(n: int) -> str:
    return f"CW{n:04d}"


def callsign(n: int) -> str:
    return f"WOC{n:04d}"
