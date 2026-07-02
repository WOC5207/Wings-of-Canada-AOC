"""Pilot roster, profiles and flight logging (PIREPs)."""
import re
import secrets
from datetime import date

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

from ..db import get_db
from ..flightnum import (TRAINING_MAX, TRAINING_MIN, callsign as route_callsign,
                        flight_no, is_training_number)
from ..security import login_required, role_required
from .dispatch import eligible_aircraft_ids

bp = Blueprint("pilots", __name__, url_prefix="/pilots")

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


@bp.route("/")
@login_required
def roster():
    rows = get_db().execute(
        """SELECT u.id, u.callsign, u.role, u.created_at,
                  COUNT(p.id) + u.adj_flights AS flights,
                  COALESCE(SUM(p.flight_time_min), 0) + u.adj_minutes AS minutes
           FROM users u
           LEFT JOIN pireps p ON p.user_id = u.id AND p.status = 'accepted'
           GROUP BY u.id
           ORDER BY minutes DESC, u.callsign"""
    ).fetchall()
    return render_template("pilots.html", pilots=rows)


@bp.route("/<int:user_id>")
@login_required
def profile(user_id):
    db = get_db()
    pilot = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if pilot is None:
        flash("Pilot not found.", "error")
        return redirect(url_for("pilots.roster"))

    logged = db.execute(
        """SELECT COUNT(*) AS flights,
                  COALESCE(SUM(flight_time_min), 0) AS minutes
           FROM pireps WHERE user_id = ? AND status = 'accepted'""",
        (user_id,),
    ).fetchone()
    # Show totals including any admin-credited flights/hours.
    stats = {
        "flights": logged["flights"] + pilot["adj_flights"],
        "minutes": logged["minutes"] + pilot["adj_minutes"],
    }
    logbook = db.execute(
        "SELECT * FROM pireps WHERE user_id = ? ORDER BY flight_date DESC, id DESC",
        (user_id,),
    ).fetchall()
    # Unique airport-pairs this pilot has flown, most recent first.
    seen, history = set(), []
    for entry in logbook:
        key = (entry["dep_icao"], entry["arr_icao"])
        if key not in seen:
            seen.add(key)
            history.append(entry)
    return render_template(
        "pilot.html", pilot=pilot, stats=stats, logbook=logbook, history=history
    )


@bp.route("/me")
@login_required
def me():
    return redirect(url_for("pilots.profile", user_id=g.user["id"]))


@bp.route("/smartcars", methods=("GET", "POST"))
@login_required
def smartcars():
    """The pilot's smartCARS 3 connection details: the Script URL, their username
    and a connection token (their api_key). A token is minted on first view so
    there is always one to copy; POST regenerates it."""
    db = get_db()
    if request.method == "POST":
        # Regenerating invalidates any smartCARS session signed in with the old
        # token (the account password keeps working regardless).
        db.execute(
            "UPDATE users SET api_key = ? WHERE id = ?",
            (secrets.token_hex(32), g.user["id"]),
        )
        db.commit()
        flash("Connection token regenerated. Sign in to smartCARS again with the "
              "new token.", "success")
        return redirect(url_for("pilots.smartcars"))

    user = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if not user["api_key"]:
        db.execute(
            "UPDATE users SET api_key = ? WHERE id = ?",
            (secrets.token_hex(32), user["id"]),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()

    # Full Script URL (honours the reverse proxy via ProxyFix), e.g.
    # https://ops.example.com/smartcars/api/
    script_url = url_for("smartcars.handshake", _external=True)
    return render_template("smartcars.html", user=user, script_url=script_url)


@bp.route("/log", methods=("GET", "POST"))
@role_required("standard")
def log_flight():
    db = get_db()
    routes = db.execute("SELECT * FROM routes ORDER BY number").fetchall()
    fleet = db.execute(
        """SELECT * FROM aircraft WHERE status = 'active'
           ORDER BY icao_type, registration"""
    ).fetchall()

    # The form opens in exactly ONE mode. "Dispatch local training" on the route
    # network passes ?mode=training; a route row's "Log flight" button passes
    # ?route_id=N (scheduled, locked to that route). A bare /pilots/log has
    # nothing to log, so send the pilot to the dispatch list to pick.
    mode = request.values.get("mode", "")
    route_id_raw = request.values.get("route_id", "")
    if mode != "training":
        mode = "scheduled" if route_id_raw else ""

    route = None
    if mode == "scheduled":
        route = next((r for r in routes if str(r["id"]) == route_id_raw), None)
        if route is None:
            flash("That route doesn't exist any more — pick one from the "
                  "dispatch list.", "error")
            return redirect(url_for("dispatch.routes"))
    elif not mode:
        flash("Pick a scheduled flight from the route network, or use "
              "“Dispatch local training” for a local session.", "error")
        return redirect(url_for("dispatch.routes"))

    # Aircraft on offer: a local training flight may use any active aircraft; a
    # scheduled flight only airframes whose load type matches the route and
    # whose range reaches the route distance (range 0 == no limit).
    allowed = (eligible_aircraft_ids(db, route["route_type"], route["distance_nm"])
               if route is not None else None)
    fleet_options = []
    for a in fleet:
        if allowed is not None and a["id"] not in allowed:
            continue
        if a["load_type"] == "cargo":
            capacity = f"{a['cargo_capacity_kg']:,} kg" if a["cargo_capacity_kg"] else ""
        else:
            capacity = f"{a['pax_capacity']} seats" if a["pax_capacity"] else ""
        fleet_options.append({
            "id": a["id"],
            "registration": a["registration"],
            "icao_type": a["icao_type"],
            "variant": a["variant"],
            "load_type": a["load_type"],
            "detail": a["variant"] or a["icao_type"],
            "capacity": capacity,
        })

    form = {
        "mode": mode,
        "route_id": route_id_raw,
        "aircraft_id": request.form.get("aircraft_id", ""),
        "airport": request.form.get("airport", "").strip().upper(),
        "training_number": request.form.get("training_number", "").strip(),
        "flight_date": request.form.get("flight_date", date.today().isoformat()),
        "hours": request.form.get("hours", "").strip(),
        "minutes": request.form.get("minutes", "").strip(),
        "remarks": request.form.get("remarks", "").strip(),
    }

    if request.method == "POST":
        errors = []
        route_id, number, dep_icao, arr_icao = None, None, "", ""

        aircraft = next(
            (a for a in fleet if str(a["id"]) == form["aircraft_id"]), None
        )
        if aircraft is None:
            errors.append("Please choose the aircraft you flew.")

        if not re.match(r"^\d{4}-\d{2}-\d{2}$", form["flight_date"]):
            errors.append("Please pick a valid flight date.")
        try:
            hours = int(form["hours"] or 0)
            minutes = int(form["minutes"] or 0)
            total_min = hours * 60 + minutes
            if not (0 <= minutes <= 59) or hours < 0:
                raise ValueError
            if total_min <= 0:
                errors.append("Flight time must be greater than zero.")
            if total_min > 24 * 60:
                errors.append("Flight time cannot exceed 24 hours.")
        except ValueError:
            errors.append("Flight time must be whole numbers (minutes 0-59).")
            total_min = 0

        if mode == "scheduled":
            route_id, number = route["id"], route["number"]
            dep_icao, arr_icao = route["dep_icao"], route["arr_icao"]
            if aircraft is not None and allowed is not None and aircraft["id"] not in allowed:
                errors.append(
                    "That aircraft isn't eligible for this route — wrong type or "
                    "not enough range."
                )
        else:  # training — a local session: one airport, any aircraft
            if not ICAO_RE.match(form["airport"]):
                errors.append("Training airport must be a 4-character ICAO code.")
            # A local training flight begins and ends at the same airport.
            dep_icao = arr_icao = form["airport"]
            try:
                number = int(form["training_number"])
            except ValueError:
                number = None
            if number is None or not is_training_number(number):
                errors.append(
                    f"Training flight number must be between {TRAINING_MIN} and {TRAINING_MAX}."
                )

        if not errors:
            db.execute(
                """INSERT INTO pireps
                   (user_id, route_id, aircraft_id, flight_no, callsign,
                    dep_icao, arr_icao, aircraft_label, flight_type, flight_date,
                    flight_time_min, remarks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.user["id"], route_id, aircraft["id"],
                 flight_no(number), route_callsign(number),
                 dep_icao, arr_icao,
                 f"{aircraft['registration']} ({aircraft['icao_type']})",
                 mode, form["flight_date"], total_min, form["remarks"]),
            )
            db.commit()
            flash(
                f"Flight {flight_no(number)} {dep_icao} → {arr_icao} logged. "
                f"Nice flying!",
                "success",
            )
            return redirect(url_for("pilots.profile", user_id=g.user["id"]))
        for e in errors:
            flash(e, "error")

    route_label = (
        f"{flight_no(route['number'])}  {route['dep_icao']} → {route['arr_icao']}"
        if route is not None else ""
    )
    return render_template(
        "pirep_new.html", form=form, fleet=fleet_options,
        route_label=route_label,
        training_min=TRAINING_MIN, training_max=TRAINING_MAX,
    )


@bp.route("/pirep/<int:pirep_id>/delete", methods=("POST",))
@login_required
def delete_pirep(pirep_id):
    db = get_db()
    row = db.execute("SELECT * FROM pireps WHERE id = ?", (pirep_id,)).fetchone()
    if row is None:
        flash("Logbook entry not found.", "error")
        return redirect(url_for("pilots.roster"))
    if row["user_id"] != g.user["id"] and g.user["role"] != "admin":
        flash("You can only delete your own logbook entries.", "error")
        return redirect(url_for("pilots.profile", user_id=row["user_id"]))
    db.execute("DELETE FROM pireps WHERE id = ?", (pirep_id,))
    db.commit()
    flash("Logbook entry deleted.", "success")
    return redirect(url_for("pilots.profile", user_id=row["user_id"]))
