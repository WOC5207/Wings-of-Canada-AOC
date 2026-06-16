"""Build dispatch routes from the imported Volanta PIREPs.

    python scripts/routes_from_imported.py [--commit]

One route per distinct leg flown, reusing the flight number already assigned to
that leg's PIREPs (so a board route matches its logged flights). Reverse legs
are paired into outbound/return; the aircraft actually flown (when in the fleet)
are approved for the route; distance/block time are great-circle estimates.

Dry run unless --commit. Idempotent: legs that already have a route are skipped.
"""
import re
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aoc.airports import estimate  # noqa: E402
from aoc.db import DB_PATH  # noqa: E402
from aoc.flightnum import flight_no  # noqa: E402

REMARK = "Imported from Volanta"
PAREN = re.compile(r"\(([^)]+)\)\s*$")


def main(argv):
    commit = "--commit" in argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    ps = conn.execute(
        "SELECT * FROM pireps WHERE remarks = ? ORDER BY flight_date, id", (REMARK,)
    ).fetchall()
    if not ps:
        print(f"no PIREPs marked '{REMARK}' - nothing to do")
        return 1
    creator = ps[0]["user_id"]

    # Existing routes: skip legs already on the board, and never reuse a number.
    existing_legs = {(r["dep_icao"].upper(), r["arr_icao"].upper())
                     for r in conn.execute("SELECT dep_icao, arr_icao FROM routes")}
    used_numbers = {r["number"] for r in conn.execute("SELECT number FROM routes")}

    # Aggregate the PIREPs into one record per leg.
    legs = {}
    for p in ps:
        key = (p["dep_icao"].upper(), p["arr_icao"].upper())
        rec = legs.setdefault(key, {
            "number": int(p["flight_no"][2:]), "types": Counter(),
            "aircraft_ids": set(), "count": 0,
        })
        rec["count"] += 1
        if p["aircraft_id"] is not None:
            rec["aircraft_ids"].add(p["aircraft_id"])
        m = PAREN.search(p["aircraft_label"] or "")
        if m:
            rec["types"][m.group(1).strip()] += 1

    # Pair reverse legs into outbound/return; the lower flight number leads.
    order = sorted(legs, key=lambda k: legs[k]["number"])
    assigned, plan = set(), []
    for leg in order:
        if leg in assigned:
            continue
        rev = (leg[1], leg[0])
        pair_id = uuid.uuid4().hex
        plan.append((leg, "outbound", pair_id))
        assigned.add(leg)
        if rev in legs and rev not in assigned:
            plan.append((rev, "return", pair_id))
            assigned.add(rev)

    rows, approvals, skipped, no_est = [], {}, 0, 0
    for leg, legkind, pair_id in plan:
        dep, arr = leg
        rec = legs[leg]
        number = rec["number"]
        if leg in existing_legs or number in used_numbers:
            skipped += 1
            continue
        used_numbers.add(number)
        actype = rec["types"].most_common(1)[0][0] if rec["types"] else ""
        est = estimate(dep, arr, actype)
        if est is None:
            no_est += 1
        rows.append((
            pair_id, legkind, number, dep, arr, actype, "pax", "",
            est["distance_nm"] if est else None,
            est["duration_min"] if est else None,
            f"Imported leg ({rec['count']} flown)", creator,
        ))
        approvals[number] = sorted(rec["aircraft_ids"])

    paired = sum(1 for _, k, _ in plan if k == "return")
    with_ac = sum(1 for n in approvals if approvals[n])
    print(f"legs: {len(legs)}  ->  routes to create: {len(rows)} "
          f"(skipped existing: {skipped})")
    print(f"reverse-paired routes: {paired}   one-way: {len(rows) - 2*paired if paired else len(rows)}")
    print(f"routes with approved fleet aircraft: {with_ac}   "
          f"no great-circle estimate (unknown airport): {no_est}")
    print("\nsample:")
    for r in rows[:6]:
        ap = approvals[r[2]]
        print(f"  {flight_no(r[2])} {r[1]:8} {r[3]}->{r[4]:5} {r[5]:10} "
              f"{(str(r[8])+'nm') if r[8] else 'no est':>8}  approved={len(ap)}")

    if not commit:
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        return 0

    for r in rows:
        cur = conn.execute(
            """INSERT INTO routes
               (pair_id, leg, number, dep_icao, arr_icao, aircraft_type,
                route_type, dep_time, distance_nm, duration_min, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            r,
        )
        rid = cur.lastrowid
        for aid in approvals[r[2]]:
            conn.execute(
                "INSERT INTO route_aircraft (route_id, aircraft_id) VALUES (?, ?)",
                (rid, aid),
            )
    conn.commit()
    print(f"\nCOMMITTED {len(rows)} routes to {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
