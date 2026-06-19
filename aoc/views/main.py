"""Public landing page and the members' dashboard - both render the same
operations overview (network stats, recent flights, leaderboard, fleet state)."""
from flask import (Blueprint, abort, g, redirect, render_template,
                   send_from_directory, url_for)

from ..db import UPLOAD_DIR, cover_media, fleet_showcase, get_db, hero_text
from ..security import login_required

bp = Blueprint("main", __name__)


def _dashboard_data(db):
    """Network overview shared by the dashboard and the public landing page:
    headline stats, fleet state, a deep recent-flights feed and the leaderboard."""
    stats = {
        "pilots": db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"],
        "fleet": db.execute(
            "SELECT COUNT(*) AS n FROM aircraft WHERE status = 'active'"
        ).fetchone()["n"],
        "routes": db.execute("SELECT COUNT(*) AS n FROM routes").fetchone()["n"],
        "hours": (
            db.execute(
                "SELECT COALESCE(SUM(flight_time_min), 0) AS n FROM pireps "
                "WHERE status = 'accepted'"
            ).fetchone()["n"]
            + db.execute(
                "SELECT COALESCE(SUM(adj_minutes), 0) AS n FROM users"
            ).fetchone()["n"]
        ),
    }
    fleet_status = {"active": 0, "maintenance": 0, "retired": 0}
    for row in db.execute(
        "SELECT status, COUNT(*) AS n FROM aircraft GROUP BY status"
    ).fetchall():
        fleet_status[row["status"]] = row["n"]
    recent_pireps = db.execute(
        """SELECT p.*, u.callsign AS pilot
           FROM pireps p JOIN users u ON u.id = p.user_id
           ORDER BY p.flight_date DESC, p.id DESC LIMIT 50"""
    ).fetchall()
    # Leaderboard: pilots with the most flight hours (logged + admin-credited).
    top_pilots = db.execute(
        """SELECT u.id, u.callsign,
                  COUNT(p.id) + u.adj_flights AS flights,
                  COALESCE(SUM(p.flight_time_min), 0) + u.adj_minutes AS minutes
           FROM users u
           LEFT JOIN pireps p ON p.user_id = u.id AND p.status = 'accepted'
           GROUP BY u.id
           HAVING minutes > 0
           ORDER BY minutes DESC, u.callsign
           LIMIT 5"""
    ).fetchall()
    return stats, fleet_status, recent_pireps, top_pilots


@bp.route("/")
def home():
    """Public landing page: a full-screen cover hero over the operations overview.
    Standard members go straight to /dashboard; admins stay so they can preview
    and change the cover, and anonymous visitors see the public page."""
    if g.user is not None and g.user["role"] != "admin":
        return redirect(url_for("main.dashboard"))
    db = get_db()
    stats, fleet_status, recent_pireps, top_pilots = _dashboard_data(db)
    return render_template(
        "home.html", stats=stats, fleet_status=fleet_status,
        recent_pireps=recent_pireps, top_pilots=top_pilots,
        cover=cover_media(db), hero=hero_text(db), showcase=fleet_showcase(db),
    )


@bp.route("/cover")
def cover():
    """Serve the current landing-page cover media (image or video)."""
    media = cover_media(get_db())
    if media is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, media["file"])


@bp.route("/dashboard")
@login_required
def dashboard():
    stats, fleet_status, recent_pireps, top_pilots = _dashboard_data(get_db())
    return render_template(
        "dashboard.html", stats=stats, fleet_status=fleet_status,
        recent_pireps=recent_pireps, top_pilots=top_pilots,
    )
