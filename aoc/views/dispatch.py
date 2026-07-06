"""Dispatch: the route network and the flight number generator."""
import re
import uuid

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

from ..airports import estimate, search as search_airports
from ..db import get_db
from ..flightnum import (SeriesFullError, allocate_one, allocate_pair,
                         callsign, flight_no)
from ..security import login_required, role_required

bp = Blueprint("dispatch", __name__, url_prefix="/dispatch")

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _route_view(row, eligible):
    """Add display fields to a routes row.

    `eligible` is the list of active aircraft (``"REGISTRATION TYPE"``) whose
    load type matches the route and whose range reaches the route distance.
    """
    r = dict(row)
    r["flight_no"] = flight_no(row["number"])
    r["callsign"] = callsign(row["number"])
    r["eligible"] = eligible
    r["simbrief_url"] = (
        "https://dispatch.simbrief.com/options/custom"
        f"?airline=WOC&fltnum={row['number']}"
        f"&orig={row['dep_icao']}&dest={row['arr_icao']}"
        + (f"&type={r['aircraft_type']}" if r["aircraft_type"] else "")
    )
    # Pre-fill SimBrief's EOBT (departure time) when the route has one.
    if r["dep_time"]:
        hh, _, mm = r["dep_time"].partition(":")
        r["simbrief_url"] += f"&deph={hh}&depm={mm}"
    return r


def eligible_aircraft_ids(db, route_type, distance_nm):
    """Active aircraft ids eligible for a route: the load type matches the
    route's pax/cargo type and the aircraft has enough range. range_nm == 0
    means "not set" and places no range limit. A NULL distance is treated as 0
    (no restriction)."""
    dist = distance_nm or 0
    rows = db.execute(
        """SELECT id FROM aircraft
           WHERE status = 'active' AND load_type = ?
             AND (range_nm = 0 OR range_nm >= ?)""",
        (route_type, dist),
    ).fetchall()
    return [r["id"] for r in rows]


def _eligible_by_route(db, rows):
    """Map route id -> list of eligible aircraft ("REGISTRATION TYPE" each),
    computed from each route's pax/cargo type and distance vs aircraft range."""
    fleet = db.execute(
        """SELECT registration, icao_type, load_type, range_nm FROM aircraft
           WHERE status = 'active' ORDER BY registration"""
    ).fetchall()
    out = {}
    for r in rows:
        dist = r["distance_nm"] or 0
        out[r["id"]] = [
            f"{a['registration']} {a['icao_type']}"
            for a in fleet
            if a["load_type"] == r["route_type"]
            and (a["range_nm"] == 0 or a["range_nm"] >= dist)
        ]
    return out


# Sortable columns (open to everyone). The callsign is "WOC" + the zero-padded
# number, so sorting by number matches sorting by callsign. NULLIF pushes routes
# with no departure time to the end (the empty string becomes NULL).
_ROUTE_SORTS = {
    "callsign": "r.number",
    "distance": "r.distance_nm",
    "dep_time": "NULLIF(r.dep_time, '')",
    "block": "r.duration_min",
}


@bp.route("/")
@login_required
def routes():
    db = get_db()
    is_admin = g.user["role"] == "admin"

    q = request.args.get("q", "").strip().upper()

    # Sorting is available to everyone; a bad value falls back to callsign asc.
    sort = request.args.get("sort", "callsign")
    if sort not in _ROUTE_SORTS:
        sort = "callsign"
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    order_dir = "DESC" if direction == "desc" else "ASC"

    where, params = [], []
    if q:
        where.append("(r.dep_icao LIKE ? OR r.arr_icao LIKE ? "
                     "OR ('CW' || printf('%04d', r.number)) LIKE ? "
                     "OR r.aircraft_type LIKE ?)")
        params += [f"%{q}%"] * 4

    # Structured filters are admin-only: departure / arrival airport, a distance
    # range, and the pilot who created the route.
    dep = arr = dist_min = dist_max = creator_id = ""
    if is_admin:
        dep = request.args.get("dep", "").strip().upper()
        arr = request.args.get("arr", "").strip().upper()
        dist_min = request.args.get("dist_min", "").strip()
        dist_max = request.args.get("dist_max", "").strip()
        creator_id = request.args.get("creator", "").strip()
        if dep:
            where.append("r.dep_icao LIKE ?")
            params.append(f"%{dep}%")
        if arr:
            where.append("r.arr_icao LIKE ?")
            params.append(f"%{arr}%")
        if dist_min.isdigit():
            where.append("r.distance_nm >= ?")
            params.append(int(dist_min))
        if dist_max.isdigit():
            where.append("r.distance_nm <= ?")
            params.append(int(dist_max))
        if creator_id.isdigit():
            where.append("r.created_by = ?")
            params.append(int(creator_id))

    col = _ROUTE_SORTS[sort]
    sql = ("SELECT r.*, u.callsign AS creator FROM routes r "
           "LEFT JOIN users u ON u.id = r.created_by")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {col} IS NULL, {col} {order_dir}, r.number"

    rows = db.execute(sql, params).fetchall()
    eligible = _eligible_by_route(db, rows)
    routes_view = [_route_view(r, eligible.get(r["id"], [])) for r in rows]

    # Pilots who have created at least one route, for the "Added by" filter.
    creators = db.execute(
        """SELECT DISTINCT u.id, u.callsign FROM users u
           JOIN routes r ON r.created_by = u.id
           ORDER BY u.callsign"""
    ).fetchall() if is_admin else []

    return render_template(
        "routes.html", routes=routes_view, q=q, sort=sort, dir=direction,
        is_admin=is_admin, dep=dep, arr=arr, dist_min=dist_min,
        dist_max=dist_max, creator_id=creator_id, creators=creators,
    )


@bp.route("/airports")
@login_required
def airport_search():
    """Autocomplete for the departure / arrival ICAO fields."""
    return {"results": search_airports(request.args.get("q", ""), limit=8)}


@bp.route("/estimate")
@login_required
def estimate_route():
    """Live distance / block-time estimate for the dispatch form."""
    dep = request.args.get("dep", "").strip().upper()
    arr = request.args.get("arr", "").strip().upper()
    actype = request.args.get("type", "").strip().upper()
    if not ICAO_RE.match(dep) or not ICAO_RE.match(arr) or dep == arr:
        return {"ok": False}
    est = estimate(dep, arr, actype)
    if est is None:
        return {"ok": False}
    minutes = est["duration_min"]
    return {
        "ok": True,
        "distance_nm": est["distance_nm"],
        "duration_min": minutes,
        "duration_hmm": f"{minutes // 60}:{minutes % 60:02d}",
    }


@bp.route("/new", methods=("GET", "POST"))
@role_required("standard")
def new_route():
    db = get_db()

    # Default the return checkbox on for a fresh form; reflect the submitted
    # value on POST (an unchecked box is simply absent from the form data).
    create_return = (request.method != "POST") or ("create_return" in request.form)
    route_type = request.form.get("route_type", "pax")
    if route_type not in ("pax", "cargo"):
        route_type = "pax"
    # The form submits the departure time as two dropdowns (hour + minute);
    # combine them into the canonical HH:MM the rest of the app stores.
    dep_h = request.form.get("dep_time_h", "").strip()
    dep_m = request.form.get("dep_time_m", "").strip()
    dep_time = f"{dep_h}:{dep_m}" if dep_h and dep_m else ""
    form = {
        "dep": request.form.get("dep", "").strip().upper(),
        "arr": request.form.get("arr", "").strip().upper(),
        "route_type": route_type,
        "dep_time": dep_time,
        "dep_time_h": dep_h,
        "dep_time_m": dep_m,
        "distance_nm": request.form.get("distance_nm", "").strip(),
        "duration": request.form.get("duration", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "create_return": create_return,
    }

    if request.method == "POST":
        errors = []
        if not ICAO_RE.match(form["dep"]):
            errors.append("Departure must be a 4-character ICAO code, e.g. CYVR.")
        if not ICAO_RE.match(form["arr"]):
            errors.append("Arrival must be a 4-character ICAO code, e.g. CYYZ.")
        if form["dep"] and form["dep"] == form["arr"]:
            errors.append("Departure and arrival cannot be the same airport.")

        if not form["dep_time"]:
            errors.append("A departure time is required.")
        elif not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", form["dep_time"]):
            errors.append("Departure time must be 24-hour HH:MM, e.g. 18:50.")

        distance = None
        if form["distance_nm"]:
            try:
                distance = max(0, int(form["distance_nm"]))
            except ValueError:
                errors.append("Distance must be a whole number of nautical miles.")

        duration = None
        if form["duration"]:
            m = re.match(r"^(\d{1,2}):([0-5]\d)$", form["duration"])
            if m:
                duration = int(m.group(1)) * 60 + int(m.group(2))
            else:
                errors.append("Block time must look like H:MM, e.g. 4:35.")

        # Fill in anything the pilot left blank with the route estimate. Without
        # a hand-picked airframe the block time uses a generic cruise speed.
        if not errors and (distance is None or duration is None):
            est = estimate(form["dep"], form["arr"])
            if est is not None:
                if distance is None:
                    distance = est["distance_nm"]
                if duration is None:
                    duration = est["duration_min"]

        if not errors:
            used = {
                r["number"] for r in db.execute("SELECT number FROM routes").fetchall()
            }
            try:
                # With a return leg the two numbers are a coupled pair in the
                # outbound's series: return = outbound - 1 when the route
                # departs Canada, + 1 when it departs abroad. A one-way route
                # is numbered on its own departure hub as before.
                if create_return:
                    out_n, ret_n = allocate_pair(form["dep"], form["arr"], used)
                else:
                    out_n = allocate_one(form["dep"], form["arr"], used)
                # Only the outbound leg carries the scheduled departure time;
                # the return leaves at an unspecified later time.
                legs = [("outbound", out_n, form["dep"], form["arr"], form["dep_time"])]
                if create_return:
                    legs.append(("return", ret_n, form["arr"], form["dep"], ""))
            except SeriesFullError as exc:
                errors.append(str(exc))
            else:
                pair_id = uuid.uuid4().hex
                for leg, number, dep, arr, leg_dep_time in legs:
                    db.execute(
                        """INSERT INTO routes
                           (pair_id, leg, number, dep_icao, arr_icao, aircraft_type,
                            route_type, dep_time, distance_nm, duration_min, notes,
                            created_by)
                           VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
                        (pair_id, leg, number, dep, arr,
                         route_type, leg_dep_time, distance, duration, form["notes"],
                         g.user["id"]),
                    )
                db.commit()
                if create_return:
                    flash(
                        f"Route created: {flight_no(out_n)} ({callsign(out_n)}) "
                        f"{form['dep']} → {form['arr']}, with return "
                        f"{flight_no(ret_n)} ({callsign(ret_n)}) "
                        f"{form['arr']} → {form['dep']}.",
                        "success",
                    )
                else:
                    flash(
                        f"Route created: {flight_no(out_n)} ({callsign(out_n)}) "
                        f"{form['dep']} → {form['arr']} (no return leg).",
                        "success",
                    )
                return redirect(url_for("dispatch.routes", q=""))

        for e in errors:
            flash(e, "error")

    return render_template("route_new.html", form=form)


@bp.route("/<int:route_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def edit_route(route_id):
    """Edit an existing route's dispatch details: PAX/cargo, departure time,
    distance/block time and notes. Departure, arrival and the flight number are
    fixed. Eligible airframes are computed from aircraft range vs the route
    distance, so there is no manual aircraft list."""
    db = get_db()
    route = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        flash("Route not found.", "error")
        return redirect(url_for("dispatch.routes"))

    if request.method == "POST":
        route_type = request.form.get("route_type", "pax")
        if route_type not in ("pax", "cargo"):
            route_type = "pax"
        dep_h = request.form.get("dep_time_h", "").strip()
        dep_m = request.form.get("dep_time_m", "").strip()
        dep_time = f"{dep_h}:{dep_m}" if dep_h and dep_m else ""
        form = {
            "dep": route["dep_icao"], "arr": route["arr_icao"],
            "route_type": route_type,
            "dep_time": dep_time, "dep_time_h": dep_h, "dep_time_m": dep_m,
            "distance_nm": request.form.get("distance_nm", "").strip(),
            "duration": request.form.get("duration", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
    else:
        cur_h, _, cur_m = (route["dep_time"] or "").partition(":")
        form = {
            "dep": route["dep_icao"], "arr": route["arr_icao"],
            "route_type": route["route_type"],
            "dep_time": route["dep_time"], "dep_time_h": cur_h, "dep_time_m": cur_m,
            "distance_nm": str(route["distance_nm"] or ""),
            "duration": (f"{route['duration_min'] // 60}:{route['duration_min'] % 60:02d}"
                         if route["duration_min"] else ""),
            "notes": route["notes"],
        }

    if request.method == "POST":
        errors = []
        # Departure time is optional when editing, but must be complete & valid.
        if (form["dep_time_h"] and not form["dep_time_m"]) or \
           (form["dep_time_m"] and not form["dep_time_h"]):
            errors.append("Set both the departure hour and minute, or leave both blank.")
        elif form["dep_time"] and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", form["dep_time"]):
            errors.append("Departure time must be 24-hour HH:MM, e.g. 18:50.")

        distance = None
        if form["distance_nm"]:
            try:
                distance = max(0, int(form["distance_nm"]))
            except ValueError:
                errors.append("Distance must be a whole number of nautical miles.")

        duration = None
        if form["duration"]:
            dmatch = re.match(r"^(\d{1,2}):([0-5]\d)$", form["duration"])
            if dmatch:
                duration = int(dmatch.group(1)) * 60 + int(dmatch.group(2))
            else:
                errors.append("Block time must look like H:MM, e.g. 4:35.")

        # Fill anything left blank with a fresh great-circle estimate.
        if not errors and (distance is None or duration is None):
            est = estimate(route["dep_icao"], route["arr_icao"])
            if est is not None:
                if distance is None:
                    distance = est["distance_nm"]
                if duration is None:
                    duration = est["duration_min"]

        if not errors:
            db.execute(
                """UPDATE routes SET route_type = ?, dep_time = ?,
                       distance_nm = ?, duration_min = ?, notes = ? WHERE id = ?""",
                (form["route_type"], form["dep_time"], distance,
                 duration, form["notes"], route_id),
            )
            db.commit()
            flash(f"Route {flight_no(route['number'])} {route['dep_icao']} → "
                  f"{route['arr_icao']} updated.", "success")
            return redirect(url_for("dispatch.routes", q=flight_no(route["number"])))

        for e in errors:
            flash(e, "error")

    route_view = {
        "flight_no": flight_no(route["number"]),
        "dep_icao": route["dep_icao"], "arr_icao": route["arr_icao"],
    }
    return render_template(
        "route_new.html", form=form, edit=True, route=route_view,
    )


@bp.route("/<int:route_id>/delete", methods=("POST",))
@role_required("admin")
def delete_route(route_id):
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if row is None:
        flash("Route not found.", "error")
    else:
        # Each leg is its own row (even when numbered as a pair), so delete
        # just this one; the partner keeps flying under its own number.
        db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        db.commit()
        flash(f"Route {flight_no(row['number'])} "
              f"{row['dep_icao']} → {row['arr_icao']} was deleted.", "success")
    return redirect(url_for("dispatch.routes"))


@bp.route("/bulk-delete", methods=("POST",))
@role_required("admin")
def bulk_delete_routes():
    """Delete every route ticked in the route network's selection column."""
    db = get_db()
    ids = [int(x) for x in request.form.getlist("route_ids") if x.isdigit()]
    deleted = 0
    if ids:
        marks = ",".join("?" * len(ids))
        deleted = db.execute(
            f"DELETE FROM routes WHERE id IN ({marks})", ids
        ).rowcount
        db.commit()
    if deleted:
        flash(f"Deleted {deleted} route{'s' if deleted != 1 else ''}.", "success")
    else:
        flash("Select at least one route to delete.", "error")
    return redirect(url_for("dispatch.routes"))
