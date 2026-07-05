"""Completed flights: the VA-wide logbook with the data smartCARS recorded."""
from flask import Blueprint, flash, redirect, render_template, url_for

from ..db import get_db
from ..security import login_required

bp = Blueprint("flights", __name__, url_prefix="/flights")


@bp.route("/")
@login_required
def index():
    """Every completed flight, newest first: accepted reports plus smartCARS
    filings awaiting review. Rejected reports and in-progress (prefiled)
    trackings are not completed flights, so they don't appear."""
    rows = get_db().execute(
        """SELECT p.*, u.callsign AS pilot,
                  (SELECT COUNT(*) FROM acars_positions a WHERE a.pirep_id = p.id)
                  AS position_count
           FROM pireps p JOIN users u ON u.id = p.user_id
           WHERE p.status IN ('accepted', 'pending')
           ORDER BY p.flight_date DESC, p.id DESC"""
    ).fetchall()
    return render_template("flights.html", flights=rows)


@bp.route("/<int:pirep_id>")
@login_required
def detail(pirep_id):
    """One completed flight with everything smartCARS recorded: the ACARS
    figures on the PIREP plus the in-flight position trail (empty for
    manually logged flights)."""
    db = get_db()
    flight = db.execute(
        """SELECT p.*, u.callsign AS pilot FROM pireps p
           JOIN users u ON u.id = p.user_id
           WHERE p.id = ? AND p.status IN ('accepted', 'pending')""",
        (pirep_id,),
    ).fetchone()
    if flight is None:
        flash("Flight not found.", "error")
        return redirect(url_for("flights.index"))
    positions = db.execute(
        "SELECT * FROM acars_positions WHERE pirep_id = ? ORDER BY id",
        (pirep_id,),
    ).fetchall()
    return render_template("flight_detail.html", flight=flight, positions=positions)
