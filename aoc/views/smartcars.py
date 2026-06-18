"""smartCARS 3 VA API.

A JSON/REST blueprint implementing the wire contract TFDi Design's smartCARS 3
desktop client expects from a virtual-airline backend. Unlike the rest of the
app this speaks JSON only, authenticates with a per-pilot bearer token
(``users.api_key``) rather than the session cookie, and never issues the HTML
login redirects the website uses.

Milestone 2 (see SMARTCARS-INTEGRATION.md) covers the skeleton: the handshake,
CORS/preflight handling, the bearer-token guard, and the pilot
login/resume/verify endpoints (plus statistics to exercise the guard). The data
and flight-lifecycle endpoints arrive in later milestones.

Contract reference (MIT): https://github.com/invernyx/smartcars-3-phpvms7-api
"""
import base64
import gzip
import secrets
from datetime import date
from functools import wraps

from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash

from .. import airports
from ..db import get_db
from ..flightnum import callsign as fmt_callsign, flight_no as fmt_flight_no
from ..ranks import rank_for

bp = Blueprint("smartcars", __name__, url_prefix="/smartcars/api")

# Reported in the handshake; bump when the wire contract changes. smartCARS
# Central keys off `handler` to know which response shape to expect, so we keep
# the phpVMS7 handler id it already understands.
API_VERSION = "1.0.0"
HANDLER = "phpvms7"

# Wings of Canada is a single airline. smartCARS' flight `code` is strictly three
# letters, so we use the ICAO (WOC); the IATA prefix (CW) only ever appears in the
# website's own "CW1234" display strings.
AIRLINE_ICAO = "WOC"
AIRLINE_NAME = "Wings of Canada"

STATUS_LABELS = {"active": "Active", "maintenance": "Maintenance", "retired": "Retired"}
PIREP_STATUS_LABELS = {"pending": "Pending", "accepted": "Accepted", "rejected": "Rejected"}

# CORS headers smartCARS (an Electron desktop app) needs on every response.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
}


@bp.before_request
def _short_circuit_preflight():
    """Answer a CORS preflight before any auth runs (mirrors the reference's
    SCHeaders middleware). _add_cors still attaches the headers below."""
    if request.method == "OPTIONS":
        return ("", 200)


@bp.after_request
def _add_cors(response):
    response.headers.update(CORS_HEADERS)
    return response


def _error(message, status):
    """The JSON error envelope smartCARS expects ({"message": ...})."""
    return jsonify({"message": message}), status


def _param(name, default=None):
    """Read a request parameter wherever smartCARS put it: JSON body, form field,
    or query string (the client mixes all three across endpoints)."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if name in data:
            return data[name]
    if name in request.values:  # form data + query string
        return request.values.get(name)
    return default


def token_required(view):
    """Guard a route with the ``Authorization: Bearer <api_key>`` scheme. On
    success the pilot row is available as ``g.sc_pilot``; otherwise 401 JSON."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        pilot = None
        if token:
            pilot = get_db().execute(
                "SELECT * FROM users WHERE api_key = ?", (token,)
            ).fetchone()
        if pilot is None:
            return _error("Invalid Token", 401)
        g.sc_pilot = pilot
        return view(*args, **kwargs)
    return wrapped


def _ensure_api_key(db, user):
    """Return the pilot's bearer token, minting and persisting one on first use."""
    if user["api_key"]:
        return user["api_key"]
    token = secrets.token_hex(32)
    db.execute("UPDATE users SET api_key = ? WHERE id = ?", (token, user["id"]))
    db.commit()
    return token


def _pilot_totals(db, user):
    """A pilot's accepted flight count and minutes, including admin-credited
    adjustments — the same basis the website stats use."""
    row = db.execute(
        "SELECT COUNT(*) AS flights, COALESCE(SUM(flight_time_min), 0) AS minutes "
        "FROM pireps WHERE user_id = ? AND status = 'accepted'",
        (user["id"],),
    ).fetchone()
    return row["flights"] + user["adj_flights"], row["minutes"] + user["adj_minutes"]


def _profile(db, user):
    """The pilot payload smartCARS expects from login/resume/verify. Field names
    mirror the phpVMS7 reference so the client renders them correctly."""
    name = (user["name"] or "").strip()
    first, _, last = name.partition(" ")
    if not first:
        first = user["callsign"]
    flights, minutes = _pilot_totals(db, user)
    return {
        "dbID": user["id"],
        "pilotID": user["callsign"],
        "firstName": first,
        "lastName": last,
        "email": user["email"] or "",
        "rank": rank_for(flights, minutes),
        "rankImage": None,
        "rankLevel": 0,
        "avatar": None,
        "session": _ensure_api_key(db, user),
    }


@bp.route("/", methods=("GET", "OPTIONS"))
def handshake():
    """Root of the Script URL. smartCARS hits this to confirm the backend is
    reachable and learn which response shape (handler) to use."""
    return jsonify({"apiVersion": API_VERSION, "handler": HANDLER})


@bp.route("/pilot/login", methods=("POST", "OPTIONS"))
def login():
    """Authenticate by callsign or email + password; return the pilot profile
    and a session token (minted on first login)."""
    username = (_param("username") or "").strip()
    password = _param("password") or ""
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE email = ? OR callsign = ?", (username, username)
    ).fetchone()
    if user is None:
        return _error("The username or password is incorrect", 401)
    # Accept the account password, or the api_key itself (lets a pilot paste
    # their token in the password field, matching the reference behaviour).
    if check_password_hash(user["password_hash"], password) or (
        user["api_key"] and password == user["api_key"]
    ):
        return jsonify(_profile(db, user))
    return _error("The username or password is incorrect", 401)


def _session_lookup():
    """Shared by resume/verify: return the profile for a stored session token."""
    token = (_param("session") or "").strip()
    db = get_db()
    user = None
    if token:
        user = db.execute(
            "SELECT * FROM users WHERE api_key = ?", (token,)
        ).fetchone()
    if user is None:
        return _error("Invalid session", 401)
    return jsonify(_profile(db, user))


@bp.route("/pilot/resume", methods=("POST", "OPTIONS"))
def resume():
    """Resume a session from a previously issued token."""
    return _session_lookup()


@bp.route("/pilot/verify", methods=("POST", "OPTIONS"))
def verify():
    """Verify a stored token is still valid (same payload as resume)."""
    return _session_lookup()


@bp.route("/pilot/statistics", methods=("GET", "OPTIONS"))
@token_required
def statistics():
    """Aggregate stats for the signed-in pilot. Demonstrates the bearer guard."""
    db = get_db()
    pilot = g.sc_pilot
    flights, minutes = _pilot_totals(db, pilot)
    avg = db.execute(
        "SELECT AVG(landing_rate) AS lr FROM pireps "
        "WHERE user_id = ? AND status = 'accepted' AND landing_rate IS NOT NULL",
        (pilot["id"],),
    ).fetchone()
    return jsonify({
        "hoursFlown": minutes / 60,
        "flightsFlown": flights,
        "averageLandingRate": avg["lr"],
        "pirepsFiled": flights,
    })


# --------------------------------------------------------------------------- #
# Reference data (/data/*)
# --------------------------------------------------------------------------- #

def _subfleet_map(db):
    """Synthesize subfleets from the active fleet (the AOC has no subfleet table):
    one per distinct ICAO type, with an id that is stable for a given fleet. Both
    /data/subfleets and the /flights/search subfleet filter use this so their ids
    agree. Returns an ordered list of (id, icao_type)."""
    types = [r["icao_type"] for r in db.execute(
        "SELECT DISTINCT icao_type FROM aircraft "
        "WHERE status = 'active' AND icao_type <> '' ORDER BY icao_type"
    ).fetchall()]
    return list(enumerate(types, start=1))


@bp.route("/data/aircraft", methods=("GET", "POST", "OPTIONS"))
@token_required
def data_aircraft():
    """Every airframe (including non-active), so any id a flight references
    resolves in the client. Mirrors the reference's aircraft payload."""
    db = get_db()
    rows = db.execute("SELECT * FROM aircraft ORDER BY icao_type, registration").fetchall()
    out = []
    for a in rows:
        detail = a["variant"] or a["icao_type"]
        label = STATUS_LABELS.get(a["status"], a["status"].title())
        out.append({
            "id": a["id"],
            "code": a["icao_type"],
            "name": f"{detail} ({a['registration']}) | {label}",
            "status": label,
            "serviceCeiling": "40000",
            "maximumPassengers": a["pax_capacity"],
            "maximumCargo": a["cargo_capacity_kg"],
            "minimumRank": 0,
        })
    return jsonify(out)


@bp.route("/data/subfleets", methods=("GET", "OPTIONS"))
@token_required
def data_subfleets():
    """Active aircraft grouped by ICAO type. smartCARS uses the subfleet id to
    filter the schedule search."""
    db = get_db()
    out = []
    for sid, icao_type in _subfleet_map(db):
        ac = db.execute(
            "SELECT id FROM aircraft WHERE status = 'active' AND icao_type = ? "
            "ORDER BY registration",
            (icao_type,),
        ).fetchall()
        out.append({
            "id": sid,
            "name": icao_type,
            "type": icao_type,
            "airline": AIRLINE_ICAO,
            "aircraft": [a["id"] for a in ac],
        })
    return jsonify(out)


@bp.route("/data/airports", methods=("GET", "POST", "OPTIONS"))
@token_required
def data_airports():
    """Coordinates/names for every airport our routes or logbook reference. The
    AOC has no airports table — coordinates come from aoc/airports.csv."""
    db = get_db()
    icaos = set()
    for table in ("routes", "pireps"):
        for r in db.execute(f"SELECT dep_icao, arr_icao FROM {table}").fetchall():
            icaos.update((r["dep_icao"], r["arr_icao"]))
    out = []
    for icao in sorted(i for i in icaos if i):
        c = airports.coords(icao)
        if c is None:
            continue
        nfo = airports.info(icao)
        out.append({
            "id": icao,
            "code": icao,
            "name": nfo["name"] if nfo else icao,
            "latitude": c[0],
            "longitude": c[1],
        })
    return jsonify(out)


@bp.route("/data/news", methods=("GET", "OPTIONS"))
@token_required
def data_news():
    """The most recent NOTAM banner, shaped as a smartCARS news item."""
    row = get_db().execute(
        "SELECT * FROM notams ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return jsonify({"title": "", "body": "", "postedAt": None, "postedBy": AIRLINE_NAME})
    return jsonify({
        "title": f"{row['level'].title()} NOTAM",
        "body": row["text"],
        "postedAt": row["created_at"],
        "postedBy": AIRLINE_NAME,
    })


@bp.route("/data/flight_types", methods=("GET", "OPTIONS"))
@token_required
def data_flight_types():
    """The flight-type codes the AOC uses (passenger / cargo)."""
    return jsonify({"P": "Passenger", "C": "Cargo"})


# --------------------------------------------------------------------------- #
# Schedule search (/flights/search)
# --------------------------------------------------------------------------- #

def _route_aircraft_ids(db, route_id):
    """Aircraft ids approved for a route; an empty approval list means any active
    aircraft may fly it (matching the website's dispatch rule)."""
    rows = db.execute(
        "SELECT aircraft_id FROM route_aircraft WHERE route_id = ?", (route_id,)
    ).fetchall()
    if rows:
        return [r["aircraft_id"] for r in rows]
    return [r["id"] for r in db.execute(
        "SELECT id FROM aircraft WHERE status = 'active'"
    ).fetchall()]


def _add_minutes(hhmm, minutes):
    """'HH:MM' + minutes -> 'HH:MM' (wrapping past midnight), '' if no input."""
    if not hhmm or ":" not in hhmm:
        return ""
    try:
        h, m = (int(x) for x in hhmm.split(":")[:2])
    except ValueError:
        return ""
    total = (h * 60 + m + (minutes or 0)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _flight_payload(db, route):
    """One schedule entry in the shape smartCARS' search expects."""
    aircraft = _route_aircraft_ids(db, route["id"])
    dep_time = route["dep_time"] or ""
    return {
        "id": route["id"],
        "number": f"{route['number']:04d}",
        "code": AIRLINE_ICAO,
        "departureAirport": route["dep_icao"],
        "arrivalAirport": route["arr_icao"],
        "flightLevel": 0,
        "route": None,
        "distance": route["distance_nm"] or 0,
        "departureTime": dep_time or "00:00",
        "arrivalTime": _add_minutes(dep_time, route["duration_min"]) or "00:00",
        "flightTime": round((route["duration_min"] or 0) / 60, 2),
        "daysOfWeek": [],
        "type": "C" if route["route_type"] == "cargo" else "P",
        # A single approved aircraft is sent as a scalar, several as an array,
        # mirroring the reference.
        "aircraft": aircraft[0] if len(aircraft) == 1 else aircraft,
        "notes": route["notes"],
    }


@bp.route("/flights/search", methods=("GET", "OPTIONS"))
@token_required
def flights_search():
    """Search the published route network, optionally filtered by departure /
    arrival airport and subfleet."""
    db = get_db()
    limit = 100
    try:
        if _param("limit") is not None:
            limit = max(1, min(int(_param("limit")), 100))
    except (TypeError, ValueError):
        pass

    clauses, args = [], []
    dep = (_param("departureAirport") or "").strip().upper()
    arr = (_param("arrivalAirport") or "").strip().upper()
    if dep:
        clauses.append("dep_icao = ?")
        args.append(dep)
    if arr:
        clauses.append("arr_icao = ?")
        args.append(arr)

    # Optional subfleet filter: the client sends the synthesized subfleet id, which
    # we resolve back to an ICAO type and keep only routes that approve such a tail.
    type_ids = None
    sub = _param("aircraft")
    if sub:
        try:
            icao_type = dict(_subfleet_map(db)).get(int(sub))
        except (TypeError, ValueError):
            icao_type = None
        if icao_type is not None:
            type_ids = {r["id"] for r in db.execute(
                "SELECT id FROM aircraft WHERE status = 'active' AND icao_type = ?",
                (icao_type,),
            ).fetchall()}

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"SELECT * FROM routes{where} ORDER BY number LIMIT ?", (*args, limit)
    ).fetchall()
    out = []
    for r in rows:
        if type_ids is not None and not (set(_route_aircraft_ids(db, r["id"])) & type_ids):
            continue
        out.append(_flight_payload(db, r))
    return jsonify(out)


# --------------------------------------------------------------------------- #
# Logbook (/pireps/*)
# --------------------------------------------------------------------------- #

def _pirep_number(flight_no):
    """The bare numeric flight number ('CW1234' -> '1234')."""
    digits = "".join(ch for ch in (flight_no or "") if ch.isdigit())
    return digits or (flight_no or "")


def _pirep_payload(db, p):
    """One logbook entry in the shape smartCARS' search/latest expects."""
    distance = 0
    if p["route_id"]:
        rt = db.execute(
            "SELECT distance_nm FROM routes WHERE id = ?", (p["route_id"],)
        ).fetchone()
        if rt and rt["distance_nm"]:
            distance = rt["distance_nm"]
    return {
        "id": p["id"],
        "submitDate": p["created_at"],
        "airlineCode": AIRLINE_ICAO,
        "route": "",
        "number": _pirep_number(p["flight_no"]),
        "distance": distance,
        "flightType": p["flight_type"],
        "departureAirport": p["dep_icao"],
        "arrivalAirport": p["arr_icao"],
        "aircraft": p["aircraft_id"],
        "status": PIREP_STATUS_LABELS.get(p["status"], "Pending"),
        "flightTime": round(p["flight_time_min"] / 60, 2),
        "landingRate": p["landing_rate"],
        "fuelUsed": p["fuel_used"] or 0,
    }


@bp.route("/pireps/search", methods=("GET", "POST", "OPTIONS"))
@token_required
def pireps_search():
    """The signed-in pilot's filed PIREPs, newest first. In-progress (prefiled)
    flights are excluded — only completed ones belong in the logbook."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM pireps WHERE user_id = ? AND status <> 'prefiled' "
        "ORDER BY flight_date DESC, id DESC",
        (g.sc_pilot["id"],),
    ).fetchall()
    return jsonify([_pirep_payload(db, p) for p in rows])


@bp.route("/pireps/latest", methods=("GET", "POST", "OPTIONS"))
@token_required
def pireps_latest():
    """The pilot's most recent filed PIREP, or [] if they have none."""
    db = get_db()
    p = db.execute(
        "SELECT * FROM pireps WHERE user_id = ? AND status <> 'prefiled' "
        "ORDER BY flight_date DESC, id DESC LIMIT 1",
        (g.sc_pilot["id"],),
    ).fetchone()
    if p is None:
        return jsonify([])
    return jsonify(_pirep_payload(db, p))


@bp.route("/pireps/details", methods=("GET", "POST", "OPTIONS"))
@token_required
def pireps_details():
    """Position trail (and event log, once populated) for one of the pilot's own
    PIREPs."""
    db = get_db()
    p = db.execute(
        "SELECT * FROM pireps WHERE id = ? AND user_id = ?",
        (_param("id"), g.sc_pilot["id"]),
    ).fetchone()
    if p is None:
        return _error("PIREP not found", 404)
    positions = db.execute(
        "SELECT lat, lon, heading FROM acars_positions WHERE pirep_id = ? ORDER BY id",
        (p["id"],),
    ).fetchall()
    return jsonify({
        "locationData": [
            {"latitude": r["lat"], "longitude": r["lon"], "heading": r["heading"]}
            for r in positions
        ],
        "flightData": [],
    })


# --------------------------------------------------------------------------- #
# Flight lifecycle (/flights/*): book -> start -> update -> complete
# --------------------------------------------------------------------------- #
#
# The AOC schedule (routes) carries no booking state, so a smartCARS booking is a
# row in `bids`. Flying it prefiles a PIREP (status 'prefiled'); telemetry appends
# to acars_positions; completion turns the PIREP 'pending' for admin acceptance
# and clears the bid. The trackingID smartCARS round-trips as `uuid` is the
# prefiled PIREP's id.


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bid_payload(db, bid):
    """A booking in the shape smartCARS' /flights/bookings expects. Scheduled
    bids borrow their schedule details from the route; charter bids only have the
    snapshot taken when they were filed."""
    number = bid["flight_no"]
    payload = {
        "bidID": bid["id"],
        "number": number,
        "code": AIRLINE_ICAO,
        "departureAirport": bid["dep_icao"],
        "arrivalAirport": bid["arr_icao"],
        "route": None,
        "flightLevel": 0,
        "distance": 0,
        "departureTime": "00:00",
        "arrivalTime": "00:00",
        "flightTime": 0,
        "daysOfWeek": [],
        "flightID": bid["route_id"],
        "type": "C" if bid["flight_type"] == "cargo" else "P",
        "aircraft": bid["aircraft_id"],
        "notes": "",
    }
    if bid["route_id"]:
        rt = db.execute("SELECT * FROM routes WHERE id = ?", (bid["route_id"],)).fetchone()
        if rt is not None:
            dep_time = rt["dep_time"] or ""
            payload.update({
                "distance": rt["distance_nm"] or 0,
                "departureTime": dep_time or "00:00",
                "arrivalTime": _add_minutes(dep_time, rt["duration_min"]) or "00:00",
                "flightTime": round((rt["duration_min"] or 0) / 60, 2),
                "type": "C" if rt["route_type"] == "cargo" else "P",
                "notes": rt["notes"],
            })
    return payload


@bp.route("/flights/bookings", methods=("GET", "OPTIONS"))
@token_required
def flights_bookings():
    """The pilot's current bookings."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM bids WHERE user_id = ? ORDER BY id", (g.sc_pilot["id"],)
    ).fetchall()
    return jsonify([_bid_payload(db, b) for b in rows])


@bp.route("/flights/book", methods=("POST", "OPTIONS"))
@token_required
def flights_book():
    """Book a scheduled route. Re-booking the same route just updates the chosen
    aircraft rather than stacking duplicate bids."""
    db = get_db()
    route = db.execute(
        "SELECT * FROM routes WHERE id = ?", (_as_int(_param("flightID")),)
    ).fetchone()
    if route is None:
        return _error("Flight not found", 404)
    aircraft_id = _as_int(_param("aircraftID"))
    existing = db.execute(
        "SELECT * FROM bids WHERE user_id = ? AND route_id = ?",
        (g.sc_pilot["id"], route["id"]),
    ).fetchone()
    if existing is not None:
        db.execute(
            "UPDATE bids SET aircraft_id = ? WHERE id = ?", (aircraft_id, existing["id"])
        )
        db.commit()
        return jsonify({"bidID": existing["id"]})
    cur = db.execute(
        """INSERT INTO bids (user_id, route_id, aircraft_id, flight_no, dep_icao,
           arr_icao, flight_type) VALUES (?, ?, ?, ?, ?, ?, 'scheduled')""",
        (g.sc_pilot["id"], route["id"], aircraft_id, f"{route['number']:04d}",
         route["dep_icao"], route["arr_icao"]),
    )
    db.commit()
    return jsonify({"bidID": cur.lastrowid})


@bp.route("/flights/rebook", methods=("POST", "OPTIONS"))
@token_required
def flights_rebook():
    """Change the aircraft on an existing booking."""
    db = get_db()
    bid = db.execute(
        "SELECT * FROM bids WHERE id = ? AND user_id = ?",
        (_as_int(_param("bidID")), g.sc_pilot["id"]),
    ).fetchone()
    if bid is None:
        return _error("Booking not found", 404)
    db.execute(
        "UPDATE bids SET aircraft_id = ? WHERE id = ?",
        (_as_int(_param("aircraft")), bid["id"]),
    )
    db.commit()
    return jsonify({"bidID": bid["id"]})


@bp.route("/flights/charter", methods=("POST", "OPTIONS"))
@token_required
def flights_charter():
    """File an ad-hoc charter booking. The pilot supplies the number, endpoints
    and aircraft; we keep only the numeric part of the number (smartCARS picks
    it, so we don't enforce the website's 9900-9999 charter reservation here)."""
    db = get_db()
    number = "".join(ch for ch in str(_param("number") or "") if ch.isdigit())
    if not number:
        return _error("A charter flight number is required", 400)
    dep = (_param("departure") or "").strip().upper()
    arr = (_param("arrival") or "").strip().upper()
    if not dep or not arr:
        return _error("Charter departure and arrival are required", 400)
    aircraft_id = _as_int(_param("aircraft"))
    cargo = str(_param("type") or "").upper().startswith("C")
    cur = db.execute(
        """INSERT INTO bids (user_id, route_id, aircraft_id, flight_no, dep_icao,
           arr_icao, flight_type) VALUES (?, NULL, ?, ?, ?, ?, 'charter')""",
        (g.sc_pilot["id"], aircraft_id, number, dep, arr),
    )
    db.commit()
    return jsonify({"bidID": cur.lastrowid})


@bp.route("/flights/unbook", methods=("POST", "OPTIONS"))
@token_required
def flights_unbook():
    """Cancel a booking, discarding any not-yet-completed flight under it."""
    db = get_db()
    bid = db.execute(
        "SELECT * FROM bids WHERE id = ? AND user_id = ?",
        (_as_int(_param("bidID")), g.sc_pilot["id"]),
    ).fetchone()
    if bid is not None:
        # Drop a prefiled (in-progress, never completed) PIREP for this bid; its
        # acars_positions cascade away.
        db.execute(
            "DELETE FROM pireps WHERE bid_id = ? AND status = 'prefiled'", (bid["id"],)
        )
        db.execute("DELETE FROM bids WHERE id = ?", (bid["id"],))
        db.commit()
    return jsonify({"status": 200})


@bp.route("/flights/start", methods=("POST", "OPTIONS"))
@token_required
def flights_start():
    """Begin tracking a booking: prefile a PIREP and hand back its trackingID.
    Starting an already-started bid returns the same trackingID (idempotent)."""
    db = get_db()
    bid = db.execute(
        "SELECT * FROM bids WHERE id = ? AND user_id = ?",
        (_as_int(_param("bidID")), g.sc_pilot["id"]),
    ).fetchone()
    if bid is None:
        return _error("Booking not found", 404)
    if bid["aircraft_id"] is None:
        return _error("Select an aircraft for this booking before starting", 400)

    existing = db.execute(
        "SELECT id FROM pireps WHERE bid_id = ? AND status = 'prefiled'", (bid["id"],)
    ).fetchone()
    if existing is not None:
        return jsonify({"trackingID": existing["id"]})

    number = _as_int(bid["flight_no"], 0)
    aircraft = db.execute(
        "SELECT * FROM aircraft WHERE id = ?", (bid["aircraft_id"],)
    ).fetchone()
    label = (f"{aircraft['registration']} ({aircraft['icao_type']})"
             if aircraft is not None else "")
    cur = db.execute(
        """INSERT INTO pireps (user_id, route_id, aircraft_id, flight_no, callsign,
           dep_icao, arr_icao, aircraft_label, flight_type, flight_date,
           flight_time_min, status, source, bid_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'prefiled', 'smartcars', ?)""",
        (g.sc_pilot["id"], bid["route_id"], bid["aircraft_id"],
         fmt_flight_no(number), fmt_callsign(number), bid["dep_icao"], bid["arr_icao"],
         label, bid["flight_type"], date.today().isoformat(), bid["id"]),
    )
    db.commit()
    return jsonify({"trackingID": cur.lastrowid})


def _owned_prefiled_pirep(db, pirep_id):
    """A prefiled PIREP belonging to the signed-in pilot, or None."""
    return db.execute(
        "SELECT * FROM pireps WHERE id = ? AND user_id = ? AND status = 'prefiled'",
        (pirep_id, g.sc_pilot["id"]),
    ).fetchone()


@bp.route("/flights/update", methods=("POST", "OPTIONS"))
@token_required
def flights_update():
    """Record one in-flight telemetry tick onto the active PIREP's trail."""
    db = get_db()
    pirep = _owned_prefiled_pirep(db, _as_int(_param("uuid")))
    if pirep is None:
        return _error("No active flight", 404)
    db.execute(
        """INSERT INTO acars_positions (pirep_id, lat, lon, altitude, heading, gs, phase)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pirep["id"], _param("latitude"), _param("longitude"),
         _as_int(_param("altitude")), _as_int(_param("heading")),
         _as_int(_param("groundSpeed")), _param("phase")),
    )
    db.commit()
    return jsonify({"status": 200})


@bp.route("/flights/cancel", methods=("POST", "OPTIONS"))
@token_required
def flights_cancel():
    """Abandon an in-progress flight. The booking is kept so it can be re-started."""
    db = get_db()
    pirep = _owned_prefiled_pirep(db, _as_int(_param("uuid")))
    if pirep is not None:
        db.execute("DELETE FROM pireps WHERE id = ?", (pirep["id"],))
        db.commit()
    return jsonify({"status": 200})


@bp.route("/flights/complete", methods=("POST", "OPTIONS"))
@token_required
def flights_complete():
    """File the completed flight: move the PIREP to 'pending' for admin review,
    record the ACARS figures, stash the raw flight data, and clear the booking."""
    db = get_db()
    pirep = _owned_prefiled_pirep(db, _as_int(_param("uuid")))
    if pirep is None:
        return _error("No active flight", 404)

    flight_time_min = round((_param("flightTime") or 0) * 60)
    landing_rate = _as_int(_param("landingRate"))
    fuel_used = _as_int(_param("fuelUsed"))
    comments = (_param("comments") or "").strip()

    # smartCARS sends the detailed flight data base64-encoded; keep it gzipped for
    # later audit/replay (best-effort — never fail the filing over it).
    acars_raw = None
    blob = _param("flightData")
    if isinstance(blob, str) and blob:
        try:
            acars_raw = gzip.compress(base64.b64decode(blob))
        except (ValueError, TypeError):
            acars_raw = None

    db.execute(
        """UPDATE pireps SET status = 'pending', flight_time_min = ?, landing_rate = ?,
           fuel_used = ?, remarks = ?, acars_raw = ? WHERE id = ?""",
        (flight_time_min, landing_rate, fuel_used,
         comments or "Filed via smartCARS 3", acars_raw, pirep["id"]),
    )
    # The booking has served its purpose; clear it so it leaves the pilot's list.
    if pirep["bid_id"] is not None:
        db.execute("DELETE FROM bids WHERE id = ?", (pirep["bid_id"],))
    db.commit()
    return jsonify({"pirepID": pirep["id"]})
