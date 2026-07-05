# Wings of Canada — Airline Operations Centre (AOC)

A self-hosted operations centre for the Wings of Canada virtual airline
(ICAO **WOC** · IATA **CW**). Built with Python/Flask and SQLite — no
database server or external services required.

## Running it

**Locally (development / trying it out):** double-click **`run.bat`** (or run
`python app.py` inside the `.venv`). The first run creates the Python
environment automatically; after that it starts instantly. Then open
**http://localhost:8080** in your browser.

**Production (Synology NAS + your own domain):** the app ships with a
`Dockerfile` and `docker-compose.yml` for DSM Container Manager, fronted by
DSM's reverse proxy for HTTPS. Follow the step-by-step guide in
**[DEPLOY-SYNOLOGY.md](DEPLOY-SYNOLOGY.md)**.

> **Important:** the very first account registered becomes the
> **Administrator** — register yourself before sharing the link.

## Features

### Membership & tiers
- Anonymous visitors see a **public home page** with the VA's status
  (members, fleet, routes, hours, latest activity) — nothing else, and no
  way to dispatch.
- Register with email + personal callsign (**WOC** + up to 4 digits) +
  password to become a **Standard** member: fly the scheduled network, **create
  new routes**, dispatch **local training** flights, browse the fleet, log PIREPs.
- **Administrators** additionally **edit and delete routes** and manage the fleet
  and the members:
  **create pilot accounts** (email optional — handy for test accounts — with a
  set or auto-generated temporary password), change tiers, reset passwords,
  remove accounts, and **credit extra flights / hours** on top of a pilot's
  logbook (e.g. experience transferred from another VA — the logbook itself is
  never altered).
  The last administrator can never be demoted or deleted.

### Fleet
Administrators add aircraft with registration, ICAO type code, variant,
a type of Passenger / Cargo (capacity in seats for passenger, kilograms for
cargo), a maximum **range in nautical miles** (which decides the routes the
airframe is eligible for — leave blank for no limit), status (Active /
Maintenance / Retired), plus links to the SimBrief airframe profile and the
livery download.

### Flights
The **Flights** tab lists every completed flight across the VA, newest first.
Flights tracked with smartCARS show their recorded ACARS data — landing rate,
fuel burn and a point count for the in-flight position log — and each flight's
detail page includes the full telemetry trail (time, phase, altitude, heading,
ground speed, position). Manually logged flights simply leave those fields
empty.

### Pilot profiles & ranks
Each pilot has a profile showing total hours, completed flights, unique
routes flown, and their full logbook. Standard members file PIREPs
(route + aircraft + date + flight time); logbook entries are snapshotted so
history survives later fleet/route changes.

Pilots earn a **rank** from their totals (shown on the roster and profile).
A rank requires meeting **both** an hours and a flights threshold, so progress
is gated by whichever is the limiting factor:

| Rank | Hours | Flights |
| --- | --- | --- |
| Student Pilot | < 10 | < 5 |
| Private Pilot | < 25 | < 10 |
| Commercial Pilot | < 50 | < 25 |
| First Officer | < 100 | < 50 |
| Senior First Officer | < 250 | < 75 |
| Captain | < 500 | < 150 |
| Senior Captain | < 1000 | < 250 |
| Fleet Captain | ≥ 1000 | ≥ 250 |

Admin-credited hours/flights count toward the rank just like logged ones.

### Dispatch & route generation
Flights come in two kinds:

- **Scheduled** routes are created by **any pilot** on the dispatch form by
  entering departure and arrival airports — the ICAO fields **autocomplete** by
  code, airport name or city and show each airport's UTC zone. The system
  assigns the flight number
  (**CW** + 4 digits) and matching radio callsign (**WOC** + same 4 digits); a
  checkbox decides whether the **return leg** is generated (on by default), each
  leg numbered independently on its own departure city. The route is marked
  **Passenger or Cargo** and can carry a scheduled **departure time (UTC)**.
  **Eligible airframes are decided automatically**: any active aircraft of the
  matching type (PAX/Cargo) whose **range** reaches the route distance may fly it
  — set each airframe's range on the Fleet page.
  The **distance and block time are estimated automatically** from a bundled
  offline airport database (5,700 airports, great-circle distance plus a per-type
  cruise speed model) — both stay editable, and anything left blank is estimated
  server-side too. Every route row has a one-click *Plan in SimBrief* button
  pre-filled with airline code, flight number, origin, destination and the
  scheduled departure time (EOBT). **Administrators** can **Edit** or **delete**
  any route afterwards to change its Passenger/Cargo type, departure
  time, distance/block time or notes — the departure, arrival and flight number
  stay fixed. Admins can also **tick several routes at once** (with a Select all
  toggle) and delete the whole selection in one action.
- **Local Training** flights are filed ad-hoc by any pilot on the flight-log form:
  a same-airport local session with any active aircraft and a flight number in the
  reserved **9900–9999** block; the hub/parity numbering rules are ignored. The
  route network page has a **Dispatch local training** shortcut that opens the
  flight log with the training tab pre-selected.

When a pilot logs a **scheduled** flight they pick the route, then choose only
from the aircraft eligible for it (matching type, enough range).

#### Numbering rules implemented

The first digit is the hub the leg touches. When **both** endpoints are hubs,
the **departure** hub wins.

| First digit | Hub / region |
|---|---|
| 1 | Vancouver (CYVR) |
| 2 | Calgary (CYYC) |
| 3 | Edmonton (CYEG) |
| 4 | Toronto (CYYZ) — **international** legs |
| 5 | Toronto (CYYZ) — **domestic** legs |
| 6 | Montréal (CYUL) |
| 7 | any other Canadian airport (no hub touched) |
| 8 | US airport, no Canadian airport involved |
| 9 | neither Canada nor the US involved (scheduled **9000–9899** only) |

The **9900–9999** block is reserved for pilot-filed **Local Training** flights, so
the scheduled 9xxx series stops at 9899.

So `CYYZ → CYVR` is a Toronto-domestic **5xxx** number, while its return
`CYVR → CYYZ` is a Vancouver **1xxx** number — the two legs are not coupled.

Parity ("flights departing Canada must be even, including 0"): a leg
**departing Canada is even**, a leg **departing abroad is odd**. Within the
series a free number of the right parity is **picked at random** (not the lowest
free one), so numbers are spread across the series instead of clustering at
x000. Numbers are never shared between routes. Deleting a route (admin only)
removes just that leg.

## Project layout

```
app.py               entry point (waitress server, port 8080)
run.bat              one-click local launcher / first-time setup
Dockerfile           container image (used on the NAS)
docker-compose.yml   Container Manager project definition
DEPLOY-SYNOLOGY.md   step-by-step NAS + domain deployment guide
aoc/
  flightnum.py       flight-number rules (series, parity, allocation)
  airports.py        distance / block-time estimates (offline)
  airports.csv       airport coordinates (OurAirports extract, public domain)
  db.py              SQLite schema + connection handling
  security.py        login + tier decorators
  views/             auth, dashboard, dispatch, fleet, pilots, admin
templates/           Jinja2 pages
static/style.css     dark ops-centre theme
data/                created at runtime: aoc.sqlite3 + session secret
tests/               python -m tests.test_flightnum
scripts/smoke.py     end-to-end HTTP test (use against a FRESH database only)
scripts/proxy_check.py  verifies reverse-proxy mode (Secure cookies, X-Forwarded-*)
scripts/build_airports.py  regenerates aoc/airports.csv from OurAirports
scripts/preview_test.py    disposable test instance (port 8081, temp database)
```

## Notes

- Passwords are stored hashed (Werkzeug PBKDF2). Sessions are signed
  cookies; the signing key is generated once into `data/secret_key.txt`.
- Back up your VA by copying the `data/` folder.
- If port 8080 is busy, set `PORT` before starting, e.g.
  `set PORT=5000 && run.bat`.
- For flight simulation only — not affiliated with any real-world airline.
