"""Regenerate aoc/airports.csv from public-domain open datasets.

    python scripts/build_airports.py [ourairports.csv] [openflights.dat]

Both sources are downloaded automatically if no path is given.

- Coordinates, name and city come from OurAirports (broad coverage):
  https://davidmegginson.github.io/ourairports-data/airports.csv
- The UTC offset comes from OpenFlights, matched by ICAO code:
  https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat
  Airports missing from OpenFlights fall back to an estimate from longitude.

Kept: every airport with a 4-letter ICAO ident that is a large or medium
airport, or any size with scheduled service (catches small northern
Canadian strips). Output columns: icao,lat,lon,name,city,utc
"""
import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPENFLIGHTS = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
OUT = Path(__file__).resolve().parent.parent / "aoc" / "airports.csv"
ICAO_RE = re.compile(r"^[A-Z]{4}$")


def _open(arg_index, url):
    if len(sys.argv) > arg_index:
        return open(sys.argv[arg_index], newline="", encoding="utf-8")
    print(f"Downloading {url} ...")
    return io.TextIOWrapper(urllib.request.urlopen(url), encoding="utf-8")


def load_openflights_offsets(src):
    """ICAO -> UTC offset in hours (float), from the OpenFlights airports.dat.

    Columns (no header): id, name, city, country, iata, icao, lat, lon, alt,
    timezone(float hours), dst, tz_db, type, source.
    """
    offsets = {}
    for row in csv.reader(src):
        if len(row) < 10:
            continue
        icao = row[5].strip().strip('"').upper()
        if not ICAO_RE.match(icao):
            continue
        try:
            offsets[icao] = float(row[9])
        except ValueError:
            continue
    return offsets


def main():
    with _open(2, OPENFLIGHTS) as of_src:
        offsets = load_openflights_offsets(of_src)
    print(f"OpenFlights UTC offsets: {len(offsets)} airports")

    kept = 0
    with _open(1, OURAIRPORTS) as source, open(OUT, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        for row in csv.DictReader(source):
            ident = row["ident"].strip().upper()
            if not ICAO_RE.match(ident):
                continue
            if (row["type"] not in ("large_airport", "medium_airport")
                    and row["scheduled_service"] != "yes"):
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except ValueError:
                continue
            # Accurate standard-time offset where OpenFlights has it; otherwise
            # a rough estimate from longitude (15 degrees per hour).
            utc = offsets.get(ident)
            if utc is None:
                utc = round(lon / 15.0)
            # Trim a trailing ".0" so whole-hour zones read cleanly.
            utc = int(utc) if float(utc).is_integer() else utc
            writer.writerow([
                ident, f"{lat:.4f}", f"{lon:.4f}",
                row["name"].strip(), row["municipality"].strip(), utc,
            ])
            kept += 1
    print(f"Wrote {kept} airports to {OUT}")


if __name__ == "__main__":
    main()
