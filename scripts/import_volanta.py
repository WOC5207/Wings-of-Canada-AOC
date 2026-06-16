"""Import a Volanta flights.csv export into the AOC logbook.

    python scripts/import_volanta.py <flights.csv> [--commit] [--user WOC5207]

Imports only rows whose callsign starts with WOC. Flight numbers are
*regenerated* with the current rules (aoc.flightnum): the leg is classified into
its series, Canada-departing legs get an even number / abroad-departing an odd
one, and a free number is chosen at random. One number is allocated per distinct
leg, so repeated flights of a leg share it and reverse legs never collide.

Without --commit it is a dry run (prints a summary, writes nothing).
"""
import csv
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aoc.db import DB_PATH  # noqa: E402
from aoc.flightnum import allocate_one, callsign as make_callsign, flight_no  # noqa: E402

REMARK = "Imported from Volanta"
MAX_MIN = 20 * 60  # clamp a handful of corrupted Volanta durations (>20h)


def secs_of(row):
    return int(float(row["block_time_secs"] or row["flight_time_secs"] or 0))


def date_of(row):
    return (row["actual_offblock"] or row["created_at"] or "")[:10]


def arr_of(row):
    # A diverted flight actually ended at the diversion airport.
    return (row["diversion_icao"] or row["destination_icao"] or "").upper()


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    opts = {a for a in argv if a.startswith("--")}
    csv_path = args[0] if args else None
    if not csv_path:
        print("usage: import_volanta.py <flights.csv> [--commit] [--user CALLSIGN]")
        return 2
    user_cs = next((a.split("=", 1)[1] for a in argv if a.startswith("--user=")), "WOC5207")
    commit = "--commit" in opts

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    woc = [r for r in rows if (r["callsign"] or "").upper().startswith("WOC")]
    woc = [r for r in woc if r["origin_icao"] and arr_of(r) and secs_of(r) > 0]
    # Oldest first so the logbook reads chronologically.
    woc.sort(key=date_of)
    print(f"{len(woc)} WOC flights to import (of {len(rows)} rows)")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    user = conn.execute("SELECT * FROM users WHERE callsign = ?", (user_cs,)).fetchone()
    if user is None:
        print(f"!! no account with callsign {user_cs}")
        return 1
    print(f"importing under {user['callsign']} (id={user['id']})")

    already = conn.execute(
        "SELECT COUNT(*) FROM pireps WHERE remarks = ?", (REMARK,)
    ).fetchone()[0]
    if already:
        print(f"!! {already} flights already marked '{REMARK}' - aborting to avoid "
              "duplicates. Delete them first to re-import.")
        return 1

    # Fleet registration -> aircraft id, and existing routes keyed by leg.
    fleet = {a["registration"].upper(): a["id"]
             for a in conn.execute("SELECT id, registration FROM aircraft")}
    routes = {(r["dep_icao"].upper(), r["arr_icao"].upper()): r
              for r in conn.execute("SELECT id, number, dep_icao, arr_icao FROM routes")}
    used = {r["number"] for r in conn.execute("SELECT number FROM routes")}

    # Assign one number per distinct leg (reusing an existing route's number when
    # the leg already exists), regenerated with the current rules.
    rng = random.Random(20260608)
    leg_number, leg_route = {}, {}
    for r in woc:
        leg = (r["origin_icao"].upper(), arr_of(r))
        if leg in leg_number:
            continue
        if leg in routes:
            leg_number[leg] = routes[leg]["number"]
            leg_route[leg] = routes[leg]["id"]
        else:
            n = allocate_one(leg[0], leg[1], used, rng=rng)
            used.add(n)
            leg_number[leg] = n
            leg_route[leg] = None

    # Build the PIREP rows.
    pireps, total_min, series = [], 0, Counter()
    for r in woc:
        leg = (r["origin_icao"].upper(), arr_of(r))
        number = leg_number[leg]
        mins = min(round(secs_of(r) / 60), MAX_MIN)
        total_min += mins
        series[str(number)[0] + "xxx"] += 1
        reg, icao = r["aircraft_registration"], r["aircraft_icao"]
        pireps.append((
            user["id"], leg_route[leg], fleet.get((reg or "").upper()),
            flight_no(number), make_callsign(number), leg[0], leg[1],
            f"{reg} ({icao})".strip() if reg else icao,
            "scheduled", date_of(r), mins, REMARK,
        ))

    print(f"distinct legs numbered: {len(leg_number)}  "
          f"(reused existing routes: {sum(1 for v in leg_route.values() if v)})")
    print(f"by series: {dict(sorted(series.items()))}")
    print(f"total time: {total_min} min = {total_min/60:.1f} h")
    matched = sum(1 for p in pireps if p[2] is not None)
    print(f"aircraft linked to fleet: {matched}/{len(pireps)}")
    print("\nfirst 5 / last 5:")
    for p in pireps[:5] + pireps[-5:]:
        print(f"  {p[9]}  {p[3]}  {p[5]}->{p[6]:5} {p[7]:22} {p[10]:>4}min")

    if not commit:
        print("\nDRY RUN - nothing written. Re-run with --commit to import.")
        return 0

    conn.executemany(
        """INSERT INTO pireps
           (user_id, route_id, aircraft_id, flight_no, callsign, dep_icao,
            arr_icao, aircraft_label, flight_type, flight_date, flight_time_min,
            remarks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        pireps,
    )
    conn.commit()
    print(f"\nCOMMITTED {len(pireps)} flights to {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
