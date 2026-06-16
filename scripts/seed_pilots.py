"""Seed roster pilots carried over from the previous platform.

Only per-pilot TOTALS are available (hours, flights, join date) -- there are no
individual flight records to import -- so each pilot's experience is stored as
admin CREDIT (adj_minutes / adj_flights) on top of an empty logbook. That is the
"experience transferred from another VA" case the credit feature was built for.

Rank is deliberately NOT stored: the ranking system computes it from the
credited totals, so the displayed rank is recalculated, not copied from the old
platform. Idempotent -- a callsign that already exists is skipped, never
overwritten (protects real accounts such as WOC3312).

Usage:
    python -m scripts.seed_pilots [path-to-db]
        (defaults to data/aoc.sqlite3 -- pass a copy to dry-run first)
"""
import secrets
import sqlite3
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aoc.ranks import rank_for  # noqa: E402

# (callsign digits, full name, total minutes, total flights, join date YYYY-MM-DD)
# Minutes are the displayed hours * 60 (e.g. 749.3 h -> 44958 min).
PILOTS = [
    ("4560", "Nancheng Guo",   44958, 254, "2022-02-10"),
    ("985",  "Sophia Harper",  24186, 212, "2020-01-16"),
    ("6410", "Alexander Fang", 19506,  84, "2021-08-31"),
    ("1026", "Li Ning",         8256,  61, "2024-10-16"),
    ("4574", "Eric Her",        3786,  33, "2021-11-14"),
    ("1068", "Steven Wang",     3540,  26, "2026-03-03"),
    # "Crystal Ye" (WOC3312) from the old roster was intentionally not imported:
    # WOC3312 is a real, separate account. The guard below would skip it anyway.
    ("217",  "Wally Bellemare", 1794,   7, "2025-11-12"),
    ("73",   "Minh Fleming",     174,   2, "2022-02-23"),
]

ROOT = Path(__file__).resolve().parent.parent


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "aoc.sqlite3")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")

    added, skipped = [], []
    for digits, name, minutes, flights, joined in PILOTS:
        callsign = f"WOC{digits}"
        if conn.execute(
            "SELECT 1 FROM users WHERE callsign = ?", (callsign,)
        ).fetchone():
            skipped.append(callsign)
            continue
        temp = secrets.token_urlsafe(9)
        conn.execute(
            """INSERT INTO users (email, callsign, name, password_hash, role,
                                  adj_flights, adj_minutes, created_at)
               VALUES (NULL, ?, ?, ?, 'standard', ?, ?, ?)""",
            (callsign, name, generate_password_hash(temp), flights, minutes,
             f"{joined} 00:00:00"),
        )
        added.append((callsign, name, minutes, flights, rank_for(flights, minutes), temp))
    conn.commit()
    conn.close()

    print(f"DB: {db_path}")
    print(f"Added {len(added)} pilot(s):")
    for cs, name, minutes, flights, rank, temp in added:
        print(f"  {cs:8} {name:16} {minutes/60:7.1f}h {flights:>4} flt  "
              f"-> {rank:15} temp_pw={temp}")
    if skipped:
        print(f"Skipped {len(skipped)} (callsign already exists, untouched): "
              f"{', '.join(skipped)}")


if __name__ == "__main__":
    main()
