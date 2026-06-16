"""Sanity checks for the flight numbering rules. Run with:

    python -m tests.test_flightnum
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aoc.flightnum import (CHARTER_MAX, CHARTER_MIN, allocate_one, classify,
                           is_charter_number, series_range, SeriesFullError)

# A seeded RNG keeps the allocation tests deterministic despite randomization.
RNG = random.Random(1234)


def check(label, got, want):
    status = "ok" if got == want else "FAIL"
    print(f"[{status}] {label}: got {got}, want {want}")
    return got == want


def check_true(label, cond):
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    return bool(cond)


def alloc(dep, arr, used=None):
    return allocate_one(dep, arr, set(used or ()), rng=RNG)


def in_series(n, digit, parity):
    return n in series_range(digit) and n % 2 == parity


def round_trip(dep, arr, used=None):
    """Allocate an outbound + return the way dispatch does."""
    used = set(used or ())
    out = allocate_one(dep, arr, used, rng=RNG)
    ret = allocate_one(arr, dep, used | {out}, rng=RNG)
    return out, ret


def main():
    ok = True

    # --- classification: departure hub wins ------------------------------
    ok &= check("YYZ->YVR domestic (Toronto departs)", classify("CYYZ", "CYVR"), 5)
    ok &= check("YVR->YYZ domestic (Vancouver departs)", classify("CYVR", "CYYZ"), 1)
    ok &= check("YYC->YVR domestic (Calgary departs)", classify("CYYC", "CYVR"), 2)
    ok &= check("non-hub to Vancouver", classify("CYQR", "CYVR"), 1)
    ok &= check("non-hub to Toronto domestic", classify("CYQR", "CYYZ"), 5)
    ok &= check("YEG arrival", classify("CYQR", "CYEG"), 3)
    ok &= check("YUL to Paris (international)", classify("CYUL", "LFPG"), 6)
    ok &= check("other Canadian domestic", classify("CYQR", "CYWG"), 7)

    # --- Toronto 4 (international) vs 5 (domestic) ------------------------
    ok &= check("YYZ->JFK international", classify("CYYZ", "KJFK"), 4)
    ok &= check("JFK->YYZ international", classify("KJFK", "CYYZ"), 4)

    # --- non-Canadian classification -------------------------------------
    ok &= check("Canadian regional to USA", classify("CYXE", "KORD"), 7)
    ok &= check("US to Europe", classify("KJFK", "EGLL"), 8)
    ok &= check("Alaska to Japan", classify("PANC", "RJAA"), 8)
    ok &= check("Europe to Asia", classify("EGLL", "VHHH"), 9)

    # --- single-leg allocation: right series + parity --------------------
    ok &= check_true("YYZ->YVR in 5xxx even (departs Canada)",
                     in_series(alloc("CYYZ", "CYVR"), 5, 0))
    ok &= check_true("YVR->YYZ in 1xxx even (departs Canada)",
                     in_series(alloc("CYVR", "CYYZ"), 1, 0))
    ok &= check_true("YYZ->JFK in 4xxx even (departs Canada)",
                     in_series(alloc("CYYZ", "KJFK"), 4, 0))
    ok &= check_true("JFK->YYZ in 4xxx odd (departs abroad)",
                     in_series(alloc("KJFK", "CYYZ"), 4, 1))
    ok &= check_true("EGLL->VHHH in 9xxx odd (departs abroad)",
                     in_series(alloc("EGLL", "VHHH"), 9, 1))
    ok &= check_true("never reuses a used number",
                     alloc("CYYZ", "CYVR", {5000, 5002, 5004}) not in {5000, 5002, 5004})

    # --- randomization: numbers spread across the series -----------------
    samples = {alloc("CYYZ", "CYVR") for _ in range(40)}
    ok &= check_true("allocation is randomized (not always x000)",
                     len(samples) > 5 and all(in_series(n, 5, 0) for n in samples))

    # --- charter block (9900-9999) reserved, scheduled 9xxx caps at 9899 -
    ok &= check("9xxx scheduled series stops before the charter block",
                max(series_range(9)), CHARTER_MIN - 1)
    ok &= check_true("scheduled rest-of-world numbers never enter the charter block",
                     all(alloc("EGLL", "VHHH") < CHARTER_MIN for _ in range(40)))
    ok &= check_true("is_charter_number covers 9900-9999 only",
                     is_charter_number(CHARTER_MIN) and is_charter_number(CHARTER_MAX)
                     and not is_charter_number(CHARTER_MIN - 1)
                     and not is_charter_number(CHARTER_MAX + 1))

    # --- round-trips are numbered independently --------------------------
    out, ret = round_trip("CYYZ", "CYVR")
    ok &= check_true("YYZ<->YVR round-trip: Toronto 5xxx + Vancouver 1xxx",
                     in_series(out, 5, 0) and in_series(ret, 1, 0))
    out, ret = round_trip("CYYZ", "KJFK")
    ok &= check_true("YYZ<->JFK international: 4xxx even out, 4xxx odd return",
                     in_series(out, 4, 0) and in_series(ret, 4, 1) and out != ret)
    out, ret = round_trip("CYYC", "CYVR")
    ok &= check_true("YYC<->YVR round-trip uses each departure hub (2xxx, 1xxx)",
                     in_series(out, 2, 0) and in_series(ret, 1, 0))

    # --- series exhaustion raises ----------------------------------------
    used = set(range(5000, 6000))
    try:
        alloc("CYYZ", "CYVR", used)
        ok &= check("series full raises", "no error", "SeriesFullError")
    except SeriesFullError:
        ok &= check("series full raises", "SeriesFullError", "SeriesFullError")

    print()
    if ok:
        print("All flight number tests passed.")
        return 0
    print("SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
