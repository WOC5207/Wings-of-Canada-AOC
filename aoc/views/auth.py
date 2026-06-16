"""Registration, login and logout."""
import re

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import consume_invite_code, find_unused_invite, get_db

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CALLSIGN_DIGITS_RE = re.compile(r"^\d{1,4}$")


@bp.route("/register", methods=("GET", "POST"))
def register():
    if g.user is not None:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        digits = request.form.get("callsign_digits", "").strip()
        invite = request.form.get("invite", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        db = get_db()
        # The very first account becomes the administrator so the system can be
        # bootstrapped; it is exempt from the invite gate (no admin exists yet to
        # mint a code). Everyone after joins as Standard and must supply one.
        first_user = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0

        errors = []
        if not first_name:
            errors.append("Please enter your preferred first name.")
        if not last_name:
            errors.append("Please enter your preferred last name.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        if not CALLSIGN_DIGITS_RE.match(digits):
            errors.append("Callsign must be WOC followed by 1 to 4 digits.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        if password != confirm:
            errors.append("The two passwords do not match.")

        # Validate the invitation code (required for everyone but the first user).
        invite_row = None
        if not first_user:
            if not invite:
                errors.append("An invitation code is required to sign up.")
            else:
                invite_row = find_unused_invite(db, invite)
                if invite_row is None:
                    errors.append("That invitation code is invalid or has already "
                                  "been used.")

        callsign = f"WOC{digits}"
        if not errors:
            if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                errors.append("That email address is already registered.")
            if db.execute("SELECT 1 FROM users WHERE callsign = ?", (callsign,)).fetchone():
                errors.append(f"Callsign {callsign} is already taken - pick another number.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", first_name=first_name,
                                   last_name=last_name, email=email, digits=digits,
                                   invite=invite)

        role = "admin" if first_user else "standard"
        cur = db.execute(
            "INSERT INTO users (email, callsign, name, password_hash, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, callsign, f"{first_name} {last_name}",
             generate_password_hash(password), role),
        )
        if invite_row is not None:
            consume_invite_code(db, invite_row["id"], cur.lastrowid)
        db.commit()

        session.clear()
        session["user_id"] = cur.lastrowid
        if first_user:
            flash(f"Welcome aboard, {callsign}! As the first member you have "
                  "been made an Administrator.", "success")
        else:
            flash(f"Welcome aboard, {callsign}! You can now fly the network "
                  "and log your flights.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("register.html", first_name="", last_name="",
                           email="", digits="", invite="")


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user is not None:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        ident = request.form.get("ident", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ? OR callsign = ?", (ident, ident)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email/callsign or password.", "error")
            return render_template("login.html", ident=ident)

        session.clear()
        session["user_id"] = user["id"]
        nxt = request.args.get("next", "")
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("main.dashboard"))

    return render_template("login.html", ident="")


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("main.home"))
