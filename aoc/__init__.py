"""Wings of Canada - Airline Operations Centre (AOC)."""
import os
import secrets
from pathlib import Path

from flask import Flask, render_template

from . import db
from .db import DATA_DIR
from .security import load_current_user, ROLE_LABELS


def _secret_key() -> str:
    """Persist a random secret so sessions survive server restarts."""
    DATA_DIR.mkdir(exist_ok=True)
    key_file = DATA_DIR / "secret_key.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent.parent / "static"),
    )
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        # Only mark cookies Secure when served over HTTPS (e.g. behind the
        # Synology reverse proxy) - over plain http the browser would drop
        # them and sign-in would silently fail.
        SESSION_COOKIE_SECURE=os.environ.get("AOC_SECURE_COOKIES", "0") == "1",
        # Large enough for an admin-uploaded landing cover (image or short video).
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    )

    # Behind a reverse proxy (Synology DSM, nginx, ...) trust the
    # X-Forwarded-* headers so the app sees the real scheme/host/client.
    if os.environ.get("AOC_BEHIND_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    app.before_request(load_current_user)

    @app.context_processor
    def inject_site_chrome():
        """Make the site-wide NOTAM banner and footer text available to every
        template."""
        conn = db.get_db()
        return {
            "notams": db.notams(conn),
            "footer_text": db.footer_text(conn),
        }

    from .views import admin, auth, dispatch, fleet, main, pilots

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(dispatch.bp)
    app.register_blueprint(fleet.bp)
    app.register_blueprint(pilots.bp)
    app.register_blueprint(admin.bp)

    @app.template_filter("hours")
    def fmt_hours(minutes):
        minutes = int(minutes or 0)
        return f"{minutes // 60}:{minutes % 60:02d}"

    @app.template_filter("hoursdec")
    def fmt_hours_decimal(minutes):
        return f"{int(minutes or 0) / 60:.1f}"

    @app.template_filter("rolelabel")
    def fmt_role(role):
        return ROLE_LABELS.get(role, role)

    @app.template_filter("datefmt")
    def fmt_date(value):
        """Render a stored 'YYYY-MM-DD ...' timestamp as e.g. 'Nov 13, 2017'."""
        from datetime import datetime
        if not value:
            return ""
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return str(value)[:10]
        # %-d / %#d differ by platform, so format the day without a pad by hand.
        return f"{d:%b} {d.day}, {d.year}"

    from .ranks import rank_class_for, rank_for
    app.add_template_global(rank_for, "pilot_rank")
    app.add_template_global(rank_class_for, "pilot_rank_class")

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="That page does not exist."), 404

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403,
                               message="You do not have access to that page."), 403

    return app
