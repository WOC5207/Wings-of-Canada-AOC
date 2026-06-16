"""Pilot rank progression based on logged hours and flights.

A pilot must satisfy BOTH the hours and the flights requirement for a rank, so
the rank is gated by whichever metric is less advanced (the limiting factor).
For example 600 hours but only 8 flights ranks as Private Pilot, and 300
flights worth only 5 hours still ranks as Student Pilot.

Each rank below the top is a (name, hours_cap, flights_cap): a pilot reaches a
tier when hours < hours_cap and flights < flights_cap. Fleet Captain is the
catch-all once both final caps are met (>= 1000 h and >= 250 flights).
"""

# Ordered lowest to highest: (name, hours_cap, flights_cap, css_class). Caps are
# the upper bound of each rank. The css class drives a colour that warms from a
# neutral slate (junior) through the spectrum to brand red (Fleet Captain).
RANKS = [
    ("Student Pilot",    10,    5,    "rank-student"),
    ("Private Pilot",    25,    10,   "rank-private"),
    ("Commercial Pilot", 50,    25,   "rank-commercial"),
    ("First Officer",    100,   50,   "rank-first-officer"),
    ("Senior First Officer", 250, 75,  "rank-senior-officer"),
    ("Captain",          500,   150,  "rank-captain"),
    ("Senior Captain",   1000,  250,  "rank-senior-captain"),
    ("Fleet Captain",    None,  None, "rank-fleet-captain"),  # above the last caps
]

_HOUR_CAPS = [r[1] for r in RANKS[:-1]]    # [10, 25, ... 1000]
_FLIGHT_CAPS = [r[2] for r in RANKS[:-1]]  # [5, 10, ... 250]


def _tier(value, caps):
    for i, cap in enumerate(caps):
        if value < cap:
            return i
    return len(caps)  # exceeded every cap -> top tier


def _rank_index(flights, minutes):
    hours = (minutes or 0) / 60
    return min(_tier(hours, _HOUR_CAPS), _tier(flights or 0, _FLIGHT_CAPS))


def rank_for(flights, minutes):
    """Return the rank name for a pilot's total flights and logged minutes."""
    return RANKS[_rank_index(flights, minutes)][0]


def rank_class_for(flights, minutes):
    """Return the CSS class colouring the rank badge."""
    return RANKS[_rank_index(flights, minutes)][3]
