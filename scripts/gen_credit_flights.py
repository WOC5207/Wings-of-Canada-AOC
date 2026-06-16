"""Convert pilots' credited totals (users.adj_flights / adj_minutes) into actual
generated PIREPs drawn from the live route network, then zero the credit so each
pilot's displayed totals are unchanged.

Skips WOC5207 (the admin, who already has a real imported logbook). For every
other pilot we generate exactly adj_flights logbook entries whose flight times
sum to exactly adj_minutes, so flipping the credit to 0 leaves totals identical.
Dates are randomized over a recent window.
"""
import math
import random
import sqlite3
from datetime import date, timedelta

random.seed(20260616)

DB = "data/aoc.sqlite3"
SKIP_CALLSIGN = "WOC5207"
DATE_START = date(2025, 9, 1)
DATE_END = date(2026, 6, 15)
MIN_FLIGHT_MIN = 20  # floor so no generated leg is implausibly short


def rand_date():
    span = (DATE_END - DATE_START).days
    return (DATE_START + timedelta(days=random.randint(0, span))).isoformat()


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    routes = conn.execute(
        """SELECT number, dep_icao, arr_icao, aircraft_type, route_type, duration_min
           FROM routes WHERE duration_min IS NOT NULL AND duration_min > 0"""
    ).fetchall()
    fleet = conn.execute(
        "SELECT id, registration, icao_type FROM aircraft WHERE status != 'retired'"
    ).fetchall()
    by_type = {}
    for a in fleet:
        by_type.setdefault(a["icao_type"], []).append(a)

    def pick_aircraft(route_type_icao):
        cands = by_type.get(route_type_icao) or fleet
        a = random.choice(cands)
        return a["id"], f"{a['registration']} ({a['icao_type']})"

    pilots = conn.execute(
        """SELECT id, callsign, adj_flights, adj_minutes FROM users
           WHERE callsign != ? AND adj_flights > 0""",
        (SKIP_CALLSIGN,),
    ).fetchall()

    total_made = 0
    for p in pilots:
        n, target = p["adj_flights"], p["adj_minutes"]
        avg = target / n
        spread = max(avg * 0.6, 45)

        # Weight route selection toward durations near this pilot's average leg,
        # so generated times stay realistic without heavy rescaling.
        weights = [math.exp(-(((r["duration_min"] - avg) / spread) ** 2)) for r in routes]
        chosen = random.choices(routes, weights=weights, k=n)

        times = [float(r["duration_min"]) for r in chosen]
        base = sum(times)
        scale = target / base if base else 1.0
        times = [max(MIN_FLIGHT_MIN, round(t * scale)) for t in times]

        # Make the sum land exactly on the credited minutes.
        diff = target - sum(times)
        order = list(range(n))
        random.shuffle(order)
        i = 0
        while diff != 0:
            j = order[i % n]
            step = 1 if diff > 0 else -1
            if times[j] + step >= MIN_FLIGHT_MIN:
                times[j] += step
                diff -= step
            i += 1

        for r, t in zip(chosen, times):
            num = r["number"]
            ac_id, ac_label = pick_aircraft(r["aircraft_type"])
            conn.execute(
                """INSERT INTO pireps
                   (user_id, route_id, aircraft_id, flight_no, callsign,
                    dep_icao, arr_icao, aircraft_label, flight_type,
                    flight_date, flight_time_min, remarks)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p["id"], None, ac_id, f"CW{num}", f"WOC{num}",
                    r["dep_icao"], r["arr_icao"], ac_label, "scheduled",
                    rand_date(), t, "Generated from credited total",
                ),
            )

        conn.execute(
            "UPDATE users SET adj_flights = 0, adj_minutes = 0 WHERE id = ?",
            (p["id"],),
        )
        total_made += n
        print(f"{p['callsign']:>8}: generated {n} flights, {sum(times)} min "
              f"(target {target}) -> credit zeroed")

    conn.commit()
    print(f"\nTotal generated PIREPs: {total_made}")
    conn.close()


if __name__ == "__main__":
    main()
