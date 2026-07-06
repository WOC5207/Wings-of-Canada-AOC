"""Administration: create accounts, member tiers, credited totals,
password resets, account removal, logbook backfills."""
import re
import secrets
from datetime import date

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)
from werkzeug.security import generate_password_hash

from ..db import (FOOTER_DEFAULT, LOGO_VARIANTS, NOTAM_LEVELS, UPLOAD_DIR,
                  add_notam as db_add_notam, cover_media,
                  create_invite_code as db_create_invite_code,
                  delete_invite_code as db_delete_invite_code,
                  delete_notam as db_delete_notam, delete_setting, footer_text,
                  get_db, get_setting, hero_text, list_invite_codes, logo_media,
                  set_setting)
from ..flightnum import (TRAINING_MAX, TRAINING_MIN, callsign as route_callsign,
                         flight_no, is_training_number)
from ..security import ROLE_LABELS, ROLE_RANK, role_required
from .auth import CALLSIGN_DIGITS_RE, EMAIL_RE
from .dispatch import eligible_aircraft_ids

ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")

try:
    from PIL import Image
except ImportError:  # Pillow is a runtime dependency (see requirements.txt).
    Image = None

bp = Blueprint("admin", __name__, url_prefix="/admin")

COVER_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
COVER_VIDEO_EXT = {"mp4", "webm"}

# Admin logo uploads: accepted raster formats, and the height every logo is
# downscaled to (about 3x the 34px topbar display size for crisp retina output).
LOGO_IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
LOGO_MAX_HEIGHT = 120


def _save_resized_logo(file, dest):
    """Open an uploaded raster image, downscale it to LOGO_MAX_HEIGHT preserving
    aspect ratio (never upscaling), and write it out as a transparent PNG."""
    img = Image.open(file.stream).convert("RGBA")
    if img.height > LOGO_MAX_HEIGHT:
        ratio = LOGO_MAX_HEIGHT / img.height
        img = img.resize(
            (max(1, round(img.width * ratio)), LOGO_MAX_HEIGHT), Image.LANCZOS
        )
    img.save(dest, format="PNG", optimize=True)


_SORTS = {
    "hours": "total_minutes DESC, u.callsign",
    "flights": "total_flights DESC, u.callsign",
    "joined": "u.created_at DESC, u.callsign",
}


@bp.route("/users")
@role_required("admin")
def users():
    sort = request.args.get("sort", "hours")
    if sort not in _SORTS:
        sort = "hours"
    query = request.args.get("q", "").strip()

    sql = """SELECT u.*, COUNT(p.id) AS logged_flights,
                    COALESCE(SUM(p.flight_time_min), 0) AS logged_minutes,
                    COUNT(p.id) + u.adj_flights AS total_flights,
                    COALESCE(SUM(p.flight_time_min), 0) + u.adj_minutes AS total_minutes
             FROM users u
             LEFT JOIN pireps p ON p.user_id = u.id AND p.status = 'accepted'"""
    params = []
    if query:
        sql += """ WHERE u.callsign LIKE ? OR COALESCE(u.name, '') LIKE ?
                         OR COALESCE(u.email, '') LIKE ?"""
        like = f"%{query}%"
        params = [like, like, like]
    sql += f" GROUP BY u.id ORDER BY {_SORTS[sort]}"

    rows = get_db().execute(sql, params).fetchall()
    return render_template("admin_users.html", users=rows, sort=sort, q=query)


@bp.route("/users/new")
@role_required("admin")
def new_user():
    """Second-stage 'add a pilot' form (kept off the roster itself)."""
    return render_template("admin_user_new.html")


@bp.route("/users/<int:user_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def edit_user(user_id):
    """Second-stage editor for one pilot: name, email, tier, join date and the
    credited totals, all in a single form. Reads are tolerant of partial POSTs
    (any field left out keeps its current value)."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin.users"))

    if request.method == "GET":
        logged = db.execute(
            """SELECT COUNT(*) AS flights,
                      COALESCE(SUM(flight_time_min), 0) AS minutes
               FROM pireps WHERE user_id = ? AND status = 'accepted'""",
            (user_id,),
        ).fetchone()
        return render_template(
            "admin_user_edit.html", user=user,
            logged_flights=logged["flights"], logged_minutes=logged["minutes"],
        )

    name = request.form.get("name", user["name"]).strip()
    email = request.form.get("email", user["email"] or "").strip().lower()
    role = request.form.get("role", user["role"])
    join_date = request.form.get("join_date", user["created_at"][:10]).strip()

    errors = []
    if email and not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address, or leave it blank.")
    if role not in ROLE_RANK:
        errors.append("Unknown tier.")
    if user["role"] == "admin" and role != "admin" and _admin_count(db) == 1:
        errors.append("You cannot demote the only administrator.")

    try:
        from datetime import datetime
        datetime.strptime(join_date, "%Y-%m-%d")
    except ValueError:
        errors.append("Enter a valid join date (YYYY-MM-DD).")

    try:
        adj_flights = int(request.form.get("adj_flights", user["adj_flights"]) or 0)
        adj_hours = int(request.form.get("adj_hours", user["adj_minutes"] // 60) or 0)
        adj_min = int(request.form.get("adj_minutes", user["adj_minutes"] % 60) or 0)
    except ValueError:
        errors.append("Credited totals must be whole numbers.")
        adj_flights = adj_hours = adj_min = 0
    else:
        if adj_flights < 0 or adj_hours < 0 or adj_min < 0:
            errors.append("Credited totals cannot be negative.")
        if adj_min > 59:
            errors.append("Credited minutes must be between 0 and 59.")

    if not errors and email and db.execute(
        "SELECT 1 FROM users WHERE email = ? AND id <> ?", (email, user_id)
    ).fetchone():
        errors.append("That email address is already registered.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin.edit_user", user_id=user_id))

    # Preserve the original time of day; only the calendar date is editable.
    existing_time = user["created_at"][11:] or "00:00:00"
    db.execute(
        """UPDATE users
           SET name = ?, email = ?, role = ?, adj_flights = ?, adj_minutes = ?,
               created_at = ?
           WHERE id = ?""",
        (name, email or None, role, adj_flights, adj_hours * 60 + adj_min,
         f"{join_date} {existing_time}", user_id),
    )
    db.commit()
    flash(f"Saved changes for {user['callsign']}.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.route("/users/create", methods=("POST",))
@role_required("admin")
def create_user():
    """Create a pilot account on behalf of someone else (no sign-in side
    effect, unlike public registration). A blank password mints a temporary
    one the admin can hand over privately."""
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    digits = request.form.get("callsign_digits", "").strip()
    role = request.form.get("role", "standard")
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    errors = []
    if email and not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address, or leave it blank.")
    if not CALLSIGN_DIGITS_RE.match(digits):
        errors.append("Callsign must be WOC followed by 1 to 4 digits.")
    if role not in ROLE_RANK:
        errors.append("Pick a valid tier.")
    if password:
        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        if password != confirm:
            errors.append("The two passwords do not match.")

    callsign = f"WOC{digits}" if CALLSIGN_DIGITS_RE.match(digits) else ""
    db = get_db()
    if not errors:
        if email and db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            errors.append("That email address is already registered.")
        if db.execute("SELECT 1 FROM users WHERE callsign = ?", (callsign,)).fetchone():
            errors.append(f"Callsign {callsign} is already taken - pick another number.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin.new_user"))

    generated = None
    if not password:
        generated = secrets.token_urlsafe(9)
        password = generated

    db.execute(
        "INSERT INTO users (email, callsign, name, password_hash, role)"
        " VALUES (?, ?, ?, ?, ?)",
        (email or None, callsign, name, generate_password_hash(password), role),
    )
    db.commit()
    who = f"{callsign} ({email})" if email else callsign
    if generated:
        flash(f"Created {who} on the {ROLE_LABELS[role]} tier. "
              f"Temporary password: {generated} - send it privately and ask "
              "them to change it after signing in.", "success")
    else:
        flash(f"Created {who} on the {ROLE_LABELS[role]} tier.", "success")
    return redirect(url_for("admin.users"))


def _admin_count(db):
    return db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
    ).fetchone()["n"]


@bp.route("/users/<int:user_id>/reset-password", methods=("POST",))
@role_required("admin")
def reset_password(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin.users"))

    temp = secrets.token_urlsafe(9)
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(temp), user_id),
    )
    db.commit()
    flash(f"Temporary password for {user['callsign']}: {temp} - send it to them "
          "privately and ask them to change it after signing in.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/delete", methods=("POST",))
@role_required("admin")
def delete_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin.users"))
    if user["id"] == g.user["id"]:
        flash("You cannot delete your own account while signed in.", "error")
        return redirect(url_for("admin.users"))
    if user["role"] == "admin" and _admin_count(db) == 1:
        flash("You cannot delete the only administrator.", "error")
        return redirect(url_for("admin.users"))

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"{user['callsign']} ({user['email']}) was removed, along with "
          "their logbook.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/flights/new", methods=("GET", "POST"))
@role_required("admin")
def add_flight(user_id):
    """Backfill a missing flight straight into one member's logbook. Pilots
    file flights by flying them with smartCARS; this is the manual fallback
    for a report that got lost. The entry is born accepted, like any manual
    log, and counts immediately."""
    db = get_db()
    member = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin.users"))

    routes = db.execute("SELECT * FROM routes ORDER BY number").fetchall()
    fleet = db.execute(
        """SELECT * FROM aircraft WHERE status = 'active'
           ORDER BY icao_type, registration"""
    ).fetchall()

    mode = request.form.get("mode", "scheduled")
    if mode not in ("scheduled", "training"):
        mode = "scheduled"
    form = {
        "mode": mode,
        "route_id": request.form.get("route_id", ""),
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
        route, number, dep_icao, arr_icao = None, None, "", ""

        aircraft = next(
            (a for a in fleet if str(a["id"]) == form["aircraft_id"]), None
        )
        if aircraft is None:
            errors.append("Pick the aircraft that flew the flight.")

        if not re.match(r"^\d{4}-\d{2}-\d{2}$", form["flight_date"]):
            errors.append("Pick a valid flight date.")
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
            route = next(
                (r for r in routes if str(r["id"]) == form["route_id"]), None
            )
            if route is None:
                errors.append("Pick the scheduled route that was flown.")
            else:
                number = route["number"]
                dep_icao, arr_icao = route["dep_icao"], route["arr_icao"]
                # Same eligibility rule the pilot faces in smartCARS: the load
                # type must match and the airframe must have the range.
                allowed = eligible_aircraft_ids(
                    db, route["route_type"], route["distance_nm"]
                )
                if aircraft is not None and aircraft["id"] not in allowed:
                    errors.append(
                        "That aircraft isn't eligible for this route — wrong "
                        "type or not enough range."
                    )
        else:  # training — a local session: one airport, any active aircraft
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
                    f"Training flight number must be between {TRAINING_MIN} "
                    f"and {TRAINING_MAX}."
                )

        if not errors:
            db.execute(
                """INSERT INTO pireps
                   (user_id, route_id, aircraft_id, flight_no, callsign,
                    dep_icao, arr_icao, aircraft_label, flight_type, flight_date,
                    flight_time_min, remarks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (member["id"], route["id"] if route else None, aircraft["id"],
                 flight_no(number), route_callsign(number),
                 dep_icao, arr_icao,
                 f"{aircraft['registration']} ({aircraft['icao_type']})",
                 mode, form["flight_date"], total_min, form["remarks"]),
            )
            db.commit()
            flash(f"Flight {flight_no(number)} {dep_icao} → {arr_icao} added "
                  f"to {member['callsign']}'s logbook.", "success")
            return redirect(url_for("pilots.profile", user_id=member["id"]))
        for e in errors:
            flash(e, "error")

    routes_view = [
        {"id": r["id"],
         "label": f"{flight_no(r['number'])}  {r['dep_icao']} → {r['arr_icao']}"}
        for r in routes
    ]
    fleet_view = []
    for a in fleet:
        if a["load_type"] == "cargo":
            capacity = f"{a['cargo_capacity_kg']:,} kg" if a["cargo_capacity_kg"] else ""
        else:
            capacity = f"{a['pax_capacity']} seats" if a["pax_capacity"] else ""
        label = f"{a['registration']} · {a['variant'] or a['icao_type']}"
        if capacity:
            label += f" · {capacity}"
        fleet_view.append({"id": a["id"], "label": label})

    return render_template(
        "admin_flight_new.html", member=member, form=form,
        routes=routes_view, fleet=fleet_view,
        training_min=TRAINING_MIN, training_max=TRAINING_MAX,
    )


@bp.route("/invites")
@role_required("admin")
def invites():
    """Temporary sign-up gate: mint and review single-use invitation codes that
    new members must supply when registering."""
    return render_template("admin_invites.html", codes=list_invite_codes(get_db()))


@bp.route("/invites/create", methods=("POST",))
@role_required("admin")
def create_invite():
    db = get_db()
    code = db_create_invite_code(db, g.user["id"])
    db.commit()
    flash(f"New invitation code: {code} - share it privately with the new member.",
          "success")
    return redirect(url_for("admin.invites"))


@bp.route("/invites/<int:code_id>/delete", methods=("POST",))
@role_required("admin")
def delete_invite(code_id):
    db = get_db()
    db_delete_invite_code(db, code_id)
    db.commit()
    flash("Invitation code revoked.", "success")
    return redirect(url_for("admin.invites"))


@bp.route("/pireps")
@role_required("admin")
def pireps():
    """The acceptance queue: PIREPs filed by smartCARS (or any source) that are
    pending review. Accepting one makes it count toward the pilot's totals."""
    rows = get_db().execute(
        """SELECT p.*, u.callsign AS pilot FROM pireps p
           JOIN users u ON u.id = p.user_id
           WHERE p.status = 'pending'
           ORDER BY p.created_at"""
    ).fetchall()
    return render_template("admin_pireps.html", pireps=rows)


def _review_pirep(pirep_id, status, verb):
    db = get_db()
    p = db.execute(
        """SELECT p.*, u.callsign AS pilot FROM pireps p
           JOIN users u ON u.id = p.user_id WHERE p.id = ?""",
        (pirep_id,),
    ).fetchone()
    if p is None or p["status"] != "pending":
        flash("That flight report is no longer pending review.", "error")
        return redirect(url_for("admin.pireps"))
    db.execute("UPDATE pireps SET status = ? WHERE id = ?", (status, pirep_id))
    db.commit()
    flash(f"Flight {p['flight_no']} {p['dep_icao']} → {p['arr_icao']} "
          f"by {p['pilot']} {verb}.", "success")
    return redirect(url_for("admin.pireps"))


@bp.route("/pireps/<int:pirep_id>/accept", methods=("POST",))
@role_required("admin")
def accept_pirep(pirep_id):
    return _review_pirep(pirep_id, "accepted", "accepted")


@bp.route("/pireps/<int:pirep_id>/reject", methods=("POST",))
@role_required("admin")
def reject_pirep(pirep_id):
    return _review_pirep(pirep_id, "rejected", "rejected")


@bp.route("/site")
@role_required("admin")
def site():
    """Site administration: the landing-page cover and its heading text."""
    db = get_db()
    return render_template(
        "admin_site.html", cover=cover_media(db), hero=hero_text(db),
        logos=logo_media(db),
    )


@bp.route("/site/logo", methods=("POST",))
@role_required("admin")
def upload_logo():
    """Upload the topbar logo for dark and/or light mode. Each provided image is
    resized to fit the topbar and stored as a transparent PNG."""
    db = get_db()
    saved, errors = [], []
    for which in LOGO_VARIANTS:
        file = request.files.get(f"logo_{which}")
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in LOGO_IMAGE_EXT:
            errors.append(f"{which.capitalize()}-mode logo must be a PNG, JPG, "
                          "WEBP or GIF image.")
            continue
        if Image is None:
            errors.append("Image processing is unavailable on the server "
                          "(Pillow is not installed).")
            break
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"logo-{which}.png"
        try:
            _save_resized_logo(file, UPLOAD_DIR / filename)
        except Exception:
            errors.append(f"Could not read the {which}-mode image — please try a "
                          "different file.")
            continue
        set_setting(db, f"logo_{which}_file", filename)
        saved.append(which)
    db.commit()

    if saved:
        flash("Logo updated for " + " and ".join(saved) + " mode.", "success")
    elif not errors:
        errors.append("Choose a dark- or light-mode logo file to upload.")
    for e in errors:
        flash(e, "error")
    return redirect(url_for("admin.site"))


@bp.route("/site/logo/remove", methods=("POST",))
@role_required("admin")
def remove_logo():
    """Remove one logo variant, reverting that mode to the default mark."""
    which = request.form.get("which")
    if which not in LOGO_VARIANTS:
        flash("Unknown logo.", "error")
        return redirect(url_for("admin.site"))
    db = get_db()
    filename = get_setting(db, f"logo_{which}_file")
    if filename:
        path = UPLOAD_DIR / filename
        if path.exists():
            path.unlink()
        delete_setting(db, f"logo_{which}_file")
        db.commit()
    flash(f"{which.capitalize()}-mode logo removed.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/cover", methods=("POST",))
@role_required("admin")
def upload_cover():
    """Replace the landing-page cover: a single image or short video, stored in
    the uploads dir and recorded in settings."""
    db = get_db()
    file = request.files.get("cover")
    if not file or not file.filename:
        flash("Choose an image or video file to upload.", "error")
        return redirect(url_for("admin.site"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext in COVER_IMAGE_EXT:
        cover_type = "image"
    elif ext in COVER_VIDEO_EXT:
        cover_type = "video"
    else:
        flash("Unsupported file type. Use JPG, PNG, WEBP, GIF, MP4 or WEBM.",
              "error")
        return redirect(url_for("admin.site"))

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Drop any previous cover.* so a new file of a different type leaves no
    # orphan behind (the served filename always matches the settings row).
    for old in UPLOAD_DIR.glob("cover.*"):
        old.unlink()
    filename = f"cover.{ext}"
    file.save(str(UPLOAD_DIR / filename))
    set_setting(db, "cover_file", filename)
    set_setting(db, "cover_type", cover_type)
    db.commit()
    flash("Landing cover updated.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/cover/remove", methods=("POST",))
@role_required("admin")
def remove_cover():
    db = get_db()
    for old in UPLOAD_DIR.glob("cover.*"):
        old.unlink()
    delete_setting(db, "cover_file")
    delete_setting(db, "cover_type")
    db.commit()
    flash("Landing cover removed.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/footer", methods=("POST",))
@role_required("admin")
def update_footer():
    """Save the site-wide footer line. Blank restores the default."""
    db = get_db()
    text = request.form.get("footer_text", "").strip()
    set_setting(db, "footer_text", text or FOOTER_DEFAULT)
    db.commit()
    flash("Footer text updated.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/notam", methods=("POST",))
@role_required("admin")
def add_notam():
    """Add a NOTAM banner. Several may be active at once."""
    db = get_db()
    text = request.form.get("notam_text", "").strip()
    level = request.form.get("notam_level", "info")
    if not text:
        flash("Enter a message for the NOTAM banner.", "error")
        return redirect(url_for("admin.site"))
    if level not in NOTAM_LEVELS:
        level = "info"
    db_add_notam(db, text, level)
    db.commit()
    flash("NOTAM banner added.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/notam/<int:notam_id>/delete", methods=("POST",))
@role_required("admin")
def delete_notam(notam_id):
    """Remove a single NOTAM banner."""
    db = get_db()
    db_delete_notam(db, notam_id)
    db.commit()
    flash("NOTAM banner removed.", "success")
    return redirect(url_for("admin.site"))


@bp.route("/site/hero", methods=("POST",))
@role_required("admin")
def update_hero():
    """Save the landing-hero heading lines and subtitle."""
    db = get_db()
    set_setting(db, "hero_title1", request.form.get("title1", "").strip())
    set_setting(db, "hero_title2", request.form.get("title2", "").strip())
    set_setting(db, "hero_subtitle", request.form.get("subtitle", "").strip())
    db.commit()
    flash("Landing headings updated.", "success")
    return redirect(url_for("admin.site"))
