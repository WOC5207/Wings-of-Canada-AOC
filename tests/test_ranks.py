"""Sanity checks for the pilot rank progression. Run with:

    python -m tests.test_ranks
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aoc.ranks import rank_for


def check(label, flights, hours, want):
    got = rank_for(flights, hours * 60)
    status = "ok" if got == want else "FAIL"
    print(f"[{status}] {label}: got {got!r}, want {want!r}")
    return got == want


def main():
    ok = True

    # --- a brand-new pilot and each rank's "both just under the cap" case ----
    ok &= check("fresh account", 0, 0, "Student Pilot")
    ok &= check("just under Student caps", 4, 9, "Student Pilot")
    ok &= check("Private when both reach tier 1", 5, 10, "Private Pilot")
    ok &= check("Commercial mid-band", 15, 30, "Commercial Pilot")
    ok &= check("First Officer mid-band", 40, 80, "First Officer")
    ok &= check("Senior First Officer mid-band", 60, 200, "Senior First Officer")
    ok &= check("Captain mid-band", 120, 400, "Captain")
    ok &= check("Senior Captain mid-band", 200, 800, "Senior Captain")
    ok &= check("Fleet Captain once both top caps met", 250, 1000, "Fleet Captain")
    ok &= check("Fleet Captain well above", 400, 5000, "Fleet Captain")

    # --- the rank is gated by the LOWER of the two metrics (the "and") -------
    ok &= check("600 h but only 8 flights -> Private", 8, 600, "Private Pilot")
    ok &= check("600 h with 10 flights -> Commercial (10 not < 10)", 10, 600, "Commercial Pilot")
    ok &= check("300 flights but only 5 h -> Student", 300, 5, "Student Pilot")
    ok &= check("149 flights, 499 h -> Captain", 149, 499, "Captain")
    ok &= check("150 flights, 500 h -> Senior Captain", 150, 500, "Senior Captain")

    # --- minutes precision: strictly under 10 h stays Student ----------------
    ok &= check("9 h 59 m, 4 flights -> Student", 4, 9, "Student Pilot")
    got = rank_for(4, 9 * 60 + 59)
    ok &= (got == "Student Pilot")
    print(f"[{'ok' if got == 'Student Pilot' else 'FAIL'}] 599 minutes is < 10 h: got {got!r}")

    print()
    if ok:
        print("All rank tests passed.")
        return 0
    print("SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
