"""Airport coordinates and route estimates (great-circle distance, block time).

Coordinates come from aoc/airports.csv, a trimmed extract of the
public-domain OurAirports dataset (regenerate with
scripts/build_airports.py).
"""
import csv
import math
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / "airports.csv"
EARTH_RADIUS_NM = 3440.065

# Typical block cruise ground speeds in knots, by ICAO type. Anything not
# listed falls back to DEFAULT_SPEED (a generic narrowbody jet).
TYPE_SPEEDS = {
    # turboprops / regional
    "DH8A": 270, "DH8B": 270, "DH8C": 285, "DH8D": 360,
    "AT45": 250, "AT46": 250, "AT75": 275, "AT76": 275,
    "B190": 280, "SF34": 250, "SW4": 230, "C208": 160, "PC12": 250,
    "DHC6": 150, "DHC7": 230,
    # regional jets
    "CRJ2": 410, "CRJ7": 430, "CRJ9": 430, "CRJX": 430,
    "E145": 410, "E170": 430, "E175": 430, "E190": 440, "E195": 440,
    "BCS1": 440, "BCS3": 440,
    # narrowbody
    "A19N": 440, "A20N": 445, "A21N": 445,
    "A318": 440, "A319": 440, "A320": 445, "A321": 445,
    "B37M": 440, "B38M": 445, "B39M": 445,
    "B736": 430, "B737": 435, "B738": 440, "B739": 440,
    "B752": 460, "B753": 460,
    # widebody
    "A332": 470, "A333": 470, "A338": 475, "A339": 475,
    "A343": 470, "A346": 475, "A359": 480, "A35K": 480, "A388": 490,
    "B762": 460, "B763": 460, "B764": 460,
    "B772": 480, "B773": 480, "B77L": 480, "B77W": 480,
    "B788": 480, "B789": 485, "B78X": 485, "B744": 490, "B748": 490,
}
DEFAULT_SPEED = 440

# Flat allowance for taxi out/in plus the slower climb/descent phases.
GROUND_AND_CLIMB_MIN = 25

_airports = None


def _load():
    global _airports
    if _airports is None:
        _airports = {}
        try:
            with open(CSV_PATH, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if len(row) < 3:
                        continue
                    icao = row[0]
                    _airports[icao] = {
                        "icao": icao,
                        "lat": float(row[1]),
                        "lon": float(row[2]),
                        "name": row[3] if len(row) > 3 else "",
                        "city": row[4] if len(row) > 4 else "",
                        "utc": row[5] if len(row) > 5 else "",
                    }
        except OSError:
            pass  # estimates simply become unavailable
    return _airports


def coords(icao: str):
    a = _load().get(icao.upper())
    return (a["lat"], a["lon"]) if a else None


def utc_label(utc) -> str:
    """Format a numeric UTC offset as e.g. 'UTC-8' or 'UTC+5:30'."""
    try:
        v = float(utc)
    except (TypeError, ValueError):
        return ""
    sign = "+" if v >= 0 else "-"
    v = abs(v)
    hours, minutes = int(v), int(round((v - int(v)) * 60))
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def _public(a: dict) -> dict:
    return {
        "icao": a["icao"], "name": a["name"], "city": a["city"],
        "utc": a["utc"], "utc_label": utc_label(a["utc"]),
    }


def info(icao: str):
    """Name / city / UTC offset for one airport, or None if unknown."""
    a = _load().get(icao.upper())
    return _public(a) if a else None


def search(q: str, limit: int = 8):
    """Airports matching q by ICAO prefix, then ICAO/name/city substring."""
    q = (q or "").strip().upper()
    if not q:
        return []
    starts, contains = [], []
    for a in _load().values():
        icao = a["icao"]
        if icao.startswith(q):
            starts.append(a)
        elif q in icao or q in a["name"].upper() or q in a["city"].upper():
            contains.append(a)
    starts.sort(key=lambda a: a["icao"])
    contains.sort(key=lambda a: a["icao"])
    return [_public(a) for a in (starts + contains)[:limit]]


def distance_nm(dep: str, arr: str):
    """Great-circle distance, or None if either airport is unknown."""
    a, b = coords(dep), coords(arr)
    if a is None or b is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return round(2 * EARTH_RADIUS_NM * math.asin(math.sqrt(h)))


def block_minutes(dist_nm: int, icao_type: str = "") -> int:
    """Rough block time: cruise at the type's speed plus a flat allowance,
    rounded to 5 minutes."""
    speed = TYPE_SPEEDS.get((icao_type or "").strip().upper(), DEFAULT_SPEED)
    raw = dist_nm / speed * 60 + GROUND_AND_CLIMB_MIN
    return max(5, int(round(raw / 5.0) * 5))


def estimate(dep: str, arr: str, icao_type: str = ""):
    """Return {'distance_nm': int, 'duration_min': int} or None."""
    dist = distance_nm(dep, arr)
    if dist is None:
        return None
    return {"distance_nm": dist, "duration_min": block_minutes(dist, icao_type)}
