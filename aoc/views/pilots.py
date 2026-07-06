"""Pilot roster, profiles and the smartCARS connection page."""
import secrets

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

from ..db import get_db
from ..security import login_required, role_required

bp = Blueprint("pilots", __name__, url_prefix="/pilots")


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


@bp.route("/pirep/<int:pirep_id>/delete", methods=("POST",))
@role_required("admin")
def delete_pirep(pirep_id):
    """Remove a flight and its ACARS position trail (cascade). Admin-only:
    logbooks are built from smartCARS filings, so pilots can't edit theirs."""
    db = get_db()
    row = db.execute("SELECT * FROM pireps WHERE id = ?", (pirep_id,)).fetchone()
    if row is None:
        flash("Flight not found.", "error")
        return redirect(url_for("flights.index"))
    db.execute("DELETE FROM pireps WHERE id = ?", (pirep_id,))
    db.commit()
    flash(f"Flight {row['flight_no']} {row['dep_icao']} → {row['arr_icao']} "
          "deleted.", "success")
    # Deleting from the completed-flights pages returns there; the default is
    # the pilot profile whose logbook held the entry.
    if request.form.get("next") == "flights":
        return redirect(url_for("flights.index"))
    return redirect(url_for("pilots.profile", user_id=row["user_id"]))
