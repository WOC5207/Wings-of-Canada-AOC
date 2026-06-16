"""Dispatch: the route network and the flight number generator."""
import re
import uuid

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

from ..airports import estimate, search as search_airports
from ..db import get_db
from ..flightnum import SeriesFullError, allocate_one, callsign, flight_no
from ..security import login_required, role_required

bp = Blueprint("dispatch", __name__, url_prefix="/dispatch")

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _route_view(row, approved):
    """Add display fields to a routes row.

    `approved` is the list of aircraft registrations an administrator approved
    for this scheduled route (empty means any active aircraft may fly it).
    """
    r = dict(row)
    r["flight_no"] = flight_no(row["number"])
    r["callsign"] = callsign(row["number"])
    r["approved"] = approved
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


def _approved_by_route(db, route_ids):
    """Map route id -> list of approved aircraft, "REGISTRATION TYPE" each."""
    if not route_ids:
        return {}
    marks = ",".join("?" * len(route_ids))
    rows = db.execute(
        f"""SELECT ra.route_id, a.registration, a.icao_type FROM route_aircraft ra
            JOIN aircraft a ON a.id = ra.aircraft_id
            WHERE ra.route_id IN ({marks})
            ORDER BY a.registration""",
        list(route_ids),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["route_id"], []).append(
            f"{r['registration']} {r['icao_type']}"
        )
    return out


@bp.route("/")
@login_required
def routes():
    q = request.args.get("q", "").strip().upper()
    db = get_db()
    if q:
        rows = db.execute(
            """SELECT r.*, u.callsign AS creator FROM routes r
               LEFT JOIN users u ON u.id = r.created_by
               WHERE r.dep_icao LIKE ? OR r.arr_icao LIKE ?
                  OR ('CW' || printf('%04d', r.number)) LIKE ?
                  OR r.aircraft_type LIKE ?
                  OR EXISTS (SELECT 1 FROM route_aircraft ra
                             JOIN aircraft a ON a.id = ra.aircraft_id
                             WHERE ra.route_id = r.id AND a.registration LIKE ?)
               ORDER BY r.number""",
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT r.*, u.callsign AS creator FROM routes r
               LEFT JOIN users u ON u.id = r.created_by
               ORDER BY r.number"""
        ).fetchall()
    approved = _approved_by_route(db, [r["id"] for r in rows])
    routes_view = [_route_view(r, approved.get(r["id"], [])) for r in rows]
    return render_template("routes.html", routes=routes_view, q=q)


def _fleet_options(db):
    """Active fleet, one option per airframe, for the aircraft picker.

    Each option carries its load_type (pax / cargo / charter) so the dispatch
    form can group the tails into categories, plus a formatted capacity.
    """
    rows = db.execute(
        """SELECT registration, icao_type, variant, load_type,
                  pax_capacity, cargo_capacity_kg FROM aircraft
           WHERE status = 'active' ORDER BY registration"""
    ).fetchall()
    options = []
    for r in rows:
        detail = r["variant"] or r["icao_type"]
        if r["load_type"] == "cargo":
            capacity = f"{r['cargo_capacity_kg']:,} kg" if r["cargo_capacity_kg"] else ""
        else:
            capacity = f"{r['pax_capacity']} seats" if r["pax_capacity"] else ""
        options.append({
            "value": r["registration"],
            "label": f"{r['registration']} — {detail}" if detail else r["registration"],
            "detail": detail,
            "icao_type": r["icao_type"],
            "load_type": r["load_type"],
            "capacity": capacity,
        })
    return options


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
@role_required("admin")
def new_route():
    db = get_db()
    fleet_options = _fleet_options(db)

    # Default the return checkbox on for a fresh form; reflect the submitted
    # value on POST (an unchecked box is simply absent from the form data).
    create_return = (request.method != "POST") or ("create_return" in request.form)
    selected = [a.strip().upper() for a in request.form.getlist("aircraft") if a.strip()]
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
        "aircraft": selected,
        "route_type": route_type,
        "dep_time": dep_time,
        "dep_time_h": dep_h,
        "dep_time_m": dep_m,
        "distance_nm": request.form.get("distance_nm", "").strip(),
        "duration": request.form.get("duration", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "create_return": create_return,
    }

    # Resolve the approved airframes (by registration) to their fleet records.
    by_reg = {o["value"]: o for o in fleet_options}
    chosen = [by_reg[reg] for reg in selected if reg in by_reg]
    # The first approved aircraft's ICAO type drives estimates / the SimBrief link.
    aircraft_icao = chosen[0]["icao_type"] if chosen else ""
    # A cargo route may only use cargo aircraft; a passenger route may use
    # passenger or charter aircraft (charter airframes carry passengers).
    allowed_loads = {"cargo"} if route_type == "cargo" else {"pax", "charter"}

    if request.method == "POST":
        errors = []
        if not ICAO_RE.match(form["dep"]):
            errors.append("Departure must be a 4-character ICAO code, e.g. CYVR.")
        if not ICAO_RE.match(form["arr"]):
            errors.append("Arrival must be a 4-character ICAO code, e.g. CYYZ.")
        if form["dep"] and form["dep"] == form["arr"]:
            errors.append("Departure and arrival cannot be the same airport.")
        if not selected:
            errors.append("Select at least one approved aircraft for the route.")
        elif any(reg not in by_reg for reg in selected):
            errors.append("Pick approved aircraft from the fleet list.")
        elif any(o["load_type"] not in allowed_loads for o in chosen):
            kind = "cargo" if route_type == "cargo" else "passenger"
            errors.append(f"A {kind} route can only approve {kind} aircraft.")

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

        # Fill in anything the pilot left blank with the route estimate.
        if not errors and (distance is None or duration is None):
            est = estimate(form["dep"], form["arr"], aircraft_icao)
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
                # Each leg is numbered independently on its own departure hub.
                out_n = allocate_one(form["dep"], form["arr"], used)
                # Only the outbound leg carries the scheduled departure time;
                # the return leaves at an unspecified later time.
                legs = [("outbound", out_n, form["dep"], form["arr"], form["dep_time"])]
                if create_return:
                    ret_n = allocate_one(form["arr"], form["dep"], used | {out_n})
                    legs.append(("return", ret_n, form["arr"], form["dep"], ""))
            except SeriesFullError as exc:
                errors.append(str(exc))
            else:
                pair_id = uuid.uuid4().hex
                # Resolve the approved registrations to aircraft ids once.
                approved_ids = [
                    row["id"] for row in db.execute(
                        f"""SELECT id FROM aircraft WHERE registration IN
                            ({','.join('?' * len(selected))})""",
                        selected,
                    ).fetchall()
                ] if selected else []
                for leg, number, dep, arr, dep_time in legs:
                    cur = db.execute(
                        """INSERT INTO routes
                           (pair_id, leg, number, dep_icao, arr_icao, aircraft_type,
                            route_type, dep_time, distance_nm, duration_min, notes,
                            created_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (pair_id, leg, number, dep, arr, aircraft_icao,
                         route_type, dep_time, distance, duration, form["notes"],
                         g.user["id"]),
                    )
                    rid = cur.lastrowid
                    for aid in approved_ids:
                        db.execute(
                            "INSERT INTO route_aircraft (route_id, aircraft_id) VALUES (?, ?)",
                            (rid, aid),
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

    return render_template("route_new.html", form=form, fleet_options=fleet_options)


@bp.route("/<int:route_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def edit_route(route_id):
    """Edit an existing route's dispatch details: approved aircraft, PAX/cargo,
    departure time, distance/block time and notes. The departure, arrival and
    flight number are fixed (they define the route and its number)."""
    db = get_db()
    route = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        flash("Route not found.", "error")
        return redirect(url_for("dispatch.routes"))

    fleet_options = _fleet_options(db)
    by_reg = {o["value"]: o for o in fleet_options}

    if request.method == "POST":
        selected = [a.strip().upper() for a in request.form.getlist("aircraft") if a.strip()]
        route_type = request.form.get("route_type", "pax")
        if route_type not in ("pax", "cargo"):
            route_type = "pax"
        dep_h = request.form.get("dep_time_h", "").strip()
        dep_m = request.form.get("dep_time_m", "").strip()
        dep_time = f"{dep_h}:{dep_m}" if dep_h and dep_m else ""
        form = {
            "dep": route["dep_icao"], "arr": route["arr_icao"],
            "aircraft": selected, "route_type": route_type,
            "dep_time": dep_time, "dep_time_h": dep_h, "dep_time_m": dep_m,
            "distance_nm": request.form.get("distance_nm", "").strip(),
            "duration": request.form.get("duration", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
    else:
        approved_now = [r["registration"].upper() for r in db.execute(
            """SELECT a.registration FROM route_aircraft ra
               JOIN aircraft a ON a.id = ra.aircraft_id
               WHERE ra.route_id = ? ORDER BY a.registration""", (route_id,)
        ).fetchall()]
        cur_h, _, cur_m = (route["dep_time"] or "").partition(":")
        form = {
            "dep": route["dep_icao"], "arr": route["arr_icao"],
            "aircraft": approved_now, "route_type": route["route_type"],
            "dep_time": route["dep_time"], "dep_time_h": cur_h, "dep_time_m": cur_m,
            "distance_nm": str(route["distance_nm"] or ""),
            "duration": (f"{route['duration_min'] // 60}:{route['duration_min'] % 60:02d}"
                         if route["duration_min"] else ""),
            "notes": route["notes"],
        }

    chosen = [by_reg[reg] for reg in form["aircraft"] if reg in by_reg]
    # Keep the existing type if (somehow) nothing is selected on render.
    aircraft_icao = chosen[0]["icao_type"] if chosen else route["aircraft_type"]
    allowed_loads = {"cargo"} if form["route_type"] == "cargo" else {"pax", "charter"}

    if request.method == "POST":
        errors = []
        selected = form["aircraft"]
        if not selected:
            errors.append("Select at least one approved aircraft for the route.")
        elif any(reg not in by_reg for reg in selected):
            errors.append("Pick approved aircraft from the fleet list.")
        elif any(o["load_type"] not in allowed_loads for o in chosen):
            kind = "cargo" if form["route_type"] == "cargo" else "passenger"
            errors.append(f"A {kind} route can only approve {kind} aircraft.")

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
            est = estimate(route["dep_icao"], route["arr_icao"], aircraft_icao)
            if est is not None:
                if distance is None:
                    distance = est["distance_nm"]
                if duration is None:
                    duration = est["duration_min"]

        if not errors:
            db.execute(
                """UPDATE routes SET aircraft_type = ?, route_type = ?, dep_time = ?,
                       distance_nm = ?, duration_min = ?, notes = ? WHERE id = ?""",
                (aircraft_icao, form["route_type"], form["dep_time"], distance,
                 duration, form["notes"], route_id),
            )
            approved_ids = [row["id"] for row in db.execute(
                f"""SELECT id FROM aircraft WHERE registration IN
                    ({','.join('?' * len(selected))})""", selected,
            ).fetchall()] if selected else []
            db.execute("DELETE FROM route_aircraft WHERE route_id = ?", (route_id,))
            for aid in approved_ids:
                db.execute(
                    "INSERT INTO route_aircraft (route_id, aircraft_id) VALUES (?, ?)",
                    (route_id, aid),
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
        "route_new.html", form=form, fleet_options=fleet_options,
        edit=True, route=route_view,
    )


@bp.route("/<int:route_id>/delete", methods=("POST",))
@role_required("admin")
def delete_route(route_id):
    db = get_db()
    row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if row is None:
        flash("Route not found.", "error")
    else:
        # Legs are numbered independently, so delete just this one.
        db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        db.commit()
        flash(f"Route {flight_no(row['number'])} "
              f"{row['dep_icao']} → {row['arr_icao']} was deleted.", "success")
    return redirect(url_for("dispatch.routes"))
