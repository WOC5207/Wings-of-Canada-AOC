# smartCARS 3 Integration — Implementation Plan

This document describes how to make **Wings of Canada AOC** work as a virtual-airline
backend for TFDi Design's **smartCARS 3** flight tracker.

- API contract reference (MIT, read-only): <https://github.com/invernyx/smartcars-3-phpvms7-api>
  — see its `openapi.json` and `Http/Controllers/Api/*` for exact request/response shapes.
- Product docs: <https://docs.tfdidesign.com/smartcars>

## 1. How it fits together

```
Pilot's PC                         TFDi cloud                Your Synology
┌──────────────┐   pick VA    ┌──────────────────┐      ┌────────────────────┐
│ smartCARS 3  │ ───────────► │ smartCARS Central│ ───► │ AOC "Script URL"   │
│ desktop app  │              │ (VA directory)   │      │ /smartcars/api/... │
└──────────────┘              └──────────────────┘      └────────────────────┘
        │  login / search / start / update / complete (HTTPS JSON, Bearer token)
        └───────────────────────────────────────────────► (same AOC endpoints)
```

You build **the Script URL** — a JSON/REST API blueprint inside the existing Flask app.
You then register Wings of Canada once in smartCARS Central (TFDi side) and set the
Script URL to `https://<yourdomain>/smartcars/api/`. Pilots install smartCARS Central,
pick Wings of Canada, and log in with their AOC credentials.

There is **no official support for custom backends** — only phpVMS 5/7 — so we implement
the same wire contract the phpVMS7 module exposes. smartCARS Central does not care what is
behind the URL as long as the JSON matches.

## 2. Scope decisions (resolve before building)

These three choices shape the rest of the work. Defaults below are the lowest-friction path.

1. **Booking model.** smartCARS books a *bid* then flies it. The AOC has no booking concept
   (`routes` is the published schedule, pilots just log what they flew).
   - *Default:* add a lightweight `bids` table. A pilot books a route in smartCARS → row in
     `bids`; `start` prefiles against it; `complete` turns it into a `pireps` row and clears
     the bid. Charters create an ad-hoc bid with a pilot-supplied flight number.
2. **PIREP acceptance.** Today a logged flight counts immediately. smartCARS expects a
   prefile → pending → accepted lifecycle.
   - *Default:* add `status`/`state` to `pireps`; ACARS-filed PIREPs land as `pending` and an
     admin accepts them (new small admin screen). Manually logged flights stay auto-accepted
     so nothing about the current `/pilots/log` flow changes. Stats count accepted only.
3. **Live tracking depth.** Do we keep the in-flight position trail (the moving map) or only
   the final PIREP?
   - *Default:* store the trail (`acars_positions`) — it's cheap and powers a future live map,
     but the map UI itself is out of scope for v1.

## 3. Data model changes (`aoc/db.py`)

All additive; follow the existing `_migrate()` pattern (each guarded by a `PRAGMA table_info`
check) so existing databases upgrade in place.

### 3.1 `users` — API token
```sql
ALTER TABLE users ADD COLUMN api_key TEXT UNIQUE;
```
Generate with `secrets.token_hex(32)` on demand (first smartCARS login, or a "Connect
smartCARS" button on the profile page). This token is the pilot's smartCARS password/session.

### 3.2 New `bids` table
```sql
CREATE TABLE IF NOT EXISTS bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    route_id    INTEGER REFERENCES routes(id) ON DELETE SET NULL,
    aircraft_id INTEGER REFERENCES aircraft(id) ON DELETE SET NULL,
    -- snapshot so a bid survives route/aircraft edits, mirroring how pireps snapshot
    flight_no   TEXT NOT NULL,
    dep_icao    TEXT NOT NULL,
    arr_icao    TEXT NOT NULL,
    flight_type TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (flight_type IN ('scheduled', 'charter')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bids_user ON bids(user_id);
```

### 3.3 `pireps` — prefile lifecycle + ACARS fields
```sql
ALTER TABLE pireps ADD COLUMN status   TEXT NOT NULL DEFAULT 'accepted'
     CHECK (status IN ('prefiled','pending','accepted','rejected'));
ALTER TABLE pireps ADD COLUMN source   TEXT NOT NULL DEFAULT 'manual';  -- 'manual' | 'smartcars'
ALTER TABLE pireps ADD COLUMN bid_id   INTEGER;            -- nullable link to the originating bid
ALTER TABLE pireps ADD COLUMN landing_rate INTEGER;        -- fpm, nullable
ALTER TABLE pireps ADD COLUMN fuel_used    INTEGER;        -- nullable
```
Existing rows default to `accepted` / `manual`, so the current logbook and stats are unaffected.
Update the roster/profile stat queries in `aoc/views/pilots.py` to count
`WHERE status = 'accepted'` (manual logs already are).

### 3.4 New `acars_positions` table (the flight trail)
```sql
CREATE TABLE IF NOT EXISTS acars_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pirep_id    INTEGER NOT NULL REFERENCES pireps(id) ON DELETE CASCADE,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    altitude    INTEGER,
    heading     INTEGER,
    gs          INTEGER,
    phase       TEXT,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_acars_pirep ON acars_positions(pirep_id);
```
Optionally also store the final raw `flightData`/`flightLog` blob (gzipped) on the PIREP for
audit, matching what the phpVMS module does — a single `acars_raw BLOB` column is enough.

## 4. The API blueprint (`aoc/views/smartcars.py`)

New blueprint, `url_prefix="/smartcars/api"`, registered in `aoc/__init__.py` alongside the
others. It returns **JSON only** and must **not** use the session-cookie auth or the HTML
`login_required` redirects — it has its own Bearer-token guard and JSON error envelope.

### 4.1 Cross-cutting concerns
- **Auth:** an `@token_required` decorator reads `Authorization: Bearer <api_key>`, looks up
  the user, sets a request-local pilot, and returns `401 {"message": "Invalid Token"}` if absent.
  (Mirror `aoc/security.py` but JSON, no redirects.)
- **CORS / preflight:** every route accepts `OPTIONS`; an `after_request` on the blueprint sets
  `Access-Control-Allow-Origin: *`, `...-Methods: GET, POST, OPTIONS, HEAD`,
  `...-Headers: Content-Type, Authorization, X-Requested-With` and `Content-Type: application/json`.
- **Proxy header:** the install requires the `Authorization` header to survive the reverse
  proxy. The Synology DSM reverse proxy passes it by default and the app already honours
  `AOC_BEHIND_PROXY` (ProxyFix) — no change, just verify after deploy.

### 4.2 Endpoint map (AOC term ⟶ smartCARS expectation)

| Method | Path | Auth | Maps to AOC | Notes |
|---|---|---|---|---|
| GET | `/` | none | — | Handshake `{"apiVersion":"1.0.0","handler":"phpvms7"}`. Keep `handler:"phpvms7"` so Central treats us as a known shape. |
| POST | `/pilot/login` | none | `users` + Werkzeug verify | Accept `username` (callsign **or** email) + `password`. On success mint/return `api_key` as `session` plus profile. |
| POST | `/pilot/resume` | none | `users.api_key` | Look up by `session` token, return profile. |
| POST | `/pilot/verify` | none | `users.api_key` | Same as resume. |
| GET | `/pilot/statistics` | bearer | `pireps` aggregate | `{hoursFlown, flightsFlown, averageLandingRate, pirepsFiled}` (accepted only). |
| GET | `/data/aircraft` | bearer | `aircraft` (active) | id, registration, ICAO type, name. |
| GET | `/data/airports` | bearer | `aoc/airports.py` | Resolve ICAOs the client asks about. |
| GET | `/data/subfleets` | bearer | grouped `aircraft` | Synthesize one subfleet per `icao_type`; list its tails. |
| GET | `/data/news` | bearer | `notams` | Map NOTAM banners to news items. |
| GET | `/data/flight_types` | bearer | constant | `scheduled` / `charter`. |
| GET | `/flights/search` | bearer | `routes` (+ `route_aircraft`) | Schedule search w/ dep/arr filters; include approved subfleets per route. |
| POST | `/flights/book` | bearer | insert `bids` | Snapshot route into a bid. |
| POST | `/flights/unbook` | bearer | delete `bids` | |
| POST | `/flights/rebook` | bearer | update `bids` | |
| GET | `/flights/bookings` | bearer | `bids` for pilot | |
| POST | `/flights/charter` | bearer | insert ad-hoc `bids` | Pilot supplies a charter number (reuse `is_charter_number` from `aoc/flightnum.py`). |
| POST | `/flights/start` | bearer | prefile `pireps` | Create `pireps` row `status='prefiled'` from the bid; return `{"trackingID": <pirep id>}`. |
| POST | `/flights/update` | bearer | insert `acars_positions` | Per-tick telemetry (lat/lon/alt/hdg/gs/phase); update PIREP phase/flight_time. |
| POST | `/flights/complete` | bearer | finalize `pireps` | Set `status='pending'`, fill landing_rate/fuel_used/flight_time/route, store raw blob, clear bid; return `{"pirepID": <id>}`. |
| POST | `/flights/cancel` | bearer | delete prefiled `pireps` | Abandon an in-progress flight. |
| GET/POST | `/pireps/search` | bearer | `pireps` | Pilot logbook query. |
| GET/POST | `/pireps/details` | bearer | one `pirep` | |
| GET/POST | `/pireps/latest` | bearer | recent `pireps` | |

### 4.3 Profile payload (used by login/resume/verify)
Match the reference field names so the client renders correctly:
```json
{
  "dbID": 12, "pilotID": "WOC123", "firstName": "Jane", "lastName": "Doe",
  "email": "jane@example.com", "rank": "Captain", "rankImage": null,
  "rankLevel": 0, "avatar": null, "session": "<api_key>"
}
```
`pilotID` = the AOC callsign. `rank` comes from `aoc/ranks.py` `rank_for(flights, minutes)` —
no rank table needed; compute it from the pilot's accepted totals.

## 5. Small AOC-side UI additions

- **Profile / "Connect smartCARS":** show the pilot their `api_key` (mint on first view) and
  the Script URL + brief connect instructions. One template + one route in `aoc/views/pilots.py`.
- **Admin PIREP queue:** list `pireps WHERE status='pending'` with Accept / Reject actions
  (Accept → `accepted`, counts toward stats; Reject → `rejected`). New section in
  `aoc/views/admin.py`. This is the only genuinely new operator workflow.

## 6. Build order (suggested milestones)

1. **Schema migrations** (§3) — add columns/tables, update stat queries to filter `accepted`.
   Verify the app still boots and the existing logbook is unchanged.
2. **Blueprint skeleton + handshake + auth** — `GET /` handshake, token decorator, CORS,
   `/pilot/login|resume|verify`. Test with `curl` against a real pilot account.
3. **Read-only data** — `/data/*`, `/flights/search`, `/pireps/*`. Now smartCARS can log in
   and browse the schedule.
4. **Flight lifecycle** — `bids` + `/flights/book|unbook|start|update|complete|cancel|charter`.
   This is the core; test a full flight end-to-end in smartCARS.
5. **AOC UI** — Connect-smartCARS page + admin PIREP queue.
6. **Register with smartCARS Central**, deploy behind the Synology proxy, fly an acceptance test.

## 7. Testing notes

- Local: drive the endpoints with `curl`/Postman before involving the desktop client —
  every route is plain JSON + a Bearer header.
- `python -m pytest`-style checks aren't set up in this repo; at minimum add a script that
  exercises login → search → start → update → complete against a throwaway SQLite db.
- Remember waitress does **not** auto-reload — restart the server after editing the blueprint
  or templates (see project deployment notes).
- Deploy verification: hit `https://<domain>/smartcars/api/` in a browser; you must get the
  handshake JSON and the `Authorization` header must reach the app through the DSM proxy.

## 8. Effort estimate

| Piece | Rough size |
|---|---|
| Schema migrations + stat-query updates | small |
| Blueprint: auth, CORS, handshake, pilot/data/pireps (read) | medium |
| Flight lifecycle (bids + start/update/complete) | medium–large (core logic) |
| AOC UI (connect page + admin queue) | small–medium |
| Central registration + deploy + acceptance flight | small (mostly external) |

Net: a focused feature, not a rewrite — one new blueprint (~15 endpoints), ~4 migrations,
two small UI surfaces. The booking/bid lifecycle and PIREP acceptance are the parts that need
the most care because they introduce concepts the AOC doesn't have today.
