"""Fleet management. Everyone can browse; only administrators can edit."""
import re
import secrets

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_from_directory, url_for)

from ..db import AIRCRAFT_DIR, aircraft_images, get_db
from ..security import login_required, role_required

bp = Blueprint("fleet", __name__, url_prefix="/fleet")

STATUSES = ("active", "maintenance", "retired")
ICAO_TYPE_RE = re.compile(r"^[A-Z0-9]{2,4}$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_IMAGES = 5


@bp.route("/")
@login_required
def index():
    rows = get_db().execute(
        """SELECT * FROM aircraft
           ORDER BY CASE status WHEN 'active' THEN 0
                                WHEN 'maintenance' THEN 1
                                ELSE 2 END,
                    icao_type, registration"""
    ).fetchall()
    return render_template("fleet.html", fleet=rows)


def _read_form(existing=None):
    load_type = request.form.get("load_type", "pax")
    if load_type not in ("pax", "cargo", "charter"):
        load_type = "pax"
    form = {
        "registration": request.form.get("registration", "").strip().upper(),
        "icao_type": request.form.get("icao_type", "").strip().upper(),
        "variant": request.form.get("variant", "").strip(),
        "load_type": load_type,
        "capacity": request.form.get("capacity", "").strip(),
        "status": request.form.get("status", "active"),
        "simbrief_url": request.form.get("simbrief_url", "").strip(),
        "livery_url": request.form.get("livery_url", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }
    errors = []
    if not re.match(r"^[A-Z0-9-]{3,10}$", form["registration"]):
        errors.append("Registration must be 3-10 letters/digits, e.g. C-FWOC.")
    if not ICAO_TYPE_RE.match(form["icao_type"]):
        errors.append("Aircraft type must be an ICAO code, e.g. B38M or DH8D.")

    cap_label = "Cargo capacity" if load_type == "cargo" else "Passenger capacity"
    try:
        capacity = max(0, int(form["capacity"] or "0"))
    except ValueError:
        errors.append(f"{cap_label} must be a whole number.")
        capacity = 0
    # Store the number in the column that matches the load type; keep the
    # other at zero so the two are never ambiguous. Charter aircraft carry
    # passengers, so their capacity is seats like a PAX aircraft.
    form["pax_capacity"] = capacity if load_type in ("pax", "charter") else 0
    form["cargo_capacity_kg"] = capacity if load_type == "cargo" else 0

    if form["status"] not in STATUSES:
        form["status"] = "active"
    for field, label in (("simbrief_url", "SimBrief profile link"),
                         ("livery_url", "Livery link")):
        if form[field] and not URL_RE.match(form[field]):
            errors.append(f"{label} must start with http:// or https://")

    db = get_db()
    if not errors:
        clash = db.execute(
            "SELECT id FROM aircraft WHERE registration = ?",
            (form["registration"],),
        ).fetchone()
        if clash and (existing is None or clash["id"] != existing["id"]):
            errors.append(f"Registration {form['registration']} already exists in the fleet.")
    return form, errors


def _save_images(db, aircraft_id, files):
    """Save valid image uploads for an airframe, respecting the 5-image cap.
    Returns (saved, skipped) counts; empty file inputs are ignored."""
    existing = db.execute(
        "SELECT COUNT(*) AS n FROM aircraft_images WHERE aircraft_id = ?",
        (aircraft_id,),
    ).fetchone()["n"]
    saved = skipped = 0
    for file in files:
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in IMAGE_EXT or existing + saved >= MAX_IMAGES:
            skipped += 1
            continue
        AIRCRAFT_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{aircraft_id}-{secrets.token_hex(8)}.{ext}"
        file.save(str(AIRCRAFT_DIR / filename))
        db.execute(
            "INSERT INTO aircraft_images (aircraft_id, filename, position)"
            " VALUES (?, ?, ?)",
            (aircraft_id, filename, existing + saved),
        )
        saved += 1
    return saved, skipped


@bp.route("/new", methods=("GET", "POST"))
@role_required("admin")
def new():
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            db = get_db()
            cur = db.execute(
                """INSERT INTO aircraft
                   (registration, icao_type, variant, load_type, pax_capacity,
                    cargo_capacity_kg, status, simbrief_url, livery_url, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (form["registration"], form["icao_type"], form["variant"],
                 form["load_type"], form["pax_capacity"], form["cargo_capacity_kg"],
                 form["status"], form["simbrief_url"], form["livery_url"],
                 form["notes"]),
            )
            saved, skipped = _save_images(db, cur.lastrowid,
                                          request.files.getlist("images"))
            db.commit()
            msg = f"{form['registration']} added to the fleet."
            if saved:
                msg += f" {saved} image(s) uploaded."
            flash(msg, "success")
            if skipped:
                flash(f"{skipped} file(s) were skipped — not an image, or over "
                      "the 5-image limit.", "error")
            return redirect(url_for("fleet.index"))
        for e in errors:
            flash(e, "error")
        return render_template("fleet_form.html", form=form, aircraft=None)
    blank = {k: "" for k in ("registration", "icao_type", "variant", "simbrief_url",
                             "livery_url", "notes")}
    blank.update(load_type="pax", capacity="", status="active")
    return render_template("fleet_form.html", form=blank, aircraft=None)


@bp.route("/<int:aircraft_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def edit(aircraft_id):
    db = get_db()
    aircraft = db.execute(
        "SELECT * FROM aircraft WHERE id = ?", (aircraft_id,)
    ).fetchone()
    if aircraft is None:
        flash("Aircraft not found.", "error")
        return redirect(url_for("fleet.index"))

    if request.method == "POST":
        form, errors = _read_form(existing=aircraft)
        if not errors:
            db.execute(
                """UPDATE aircraft SET registration = ?, icao_type = ?, variant = ?,
                   load_type = ?, pax_capacity = ?, cargo_capacity_kg = ?, status = ?,
                   simbrief_url = ?, livery_url = ?, notes = ?
                   WHERE id = ?""",
                (form["registration"], form["icao_type"], form["variant"],
                 form["load_type"], form["pax_capacity"], form["cargo_capacity_kg"],
                 form["status"], form["simbrief_url"], form["livery_url"],
                 form["notes"], aircraft_id),
            )
            db.commit()
            flash(f"{form['registration']} updated.", "success")
            return redirect(url_for("fleet.index"))
        for e in errors:
            flash(e, "error")
        return render_template("fleet_form.html", form=form, aircraft=aircraft,
                               images=aircraft_images(db, aircraft_id))

    # Pre-fill the single capacity field from whichever column applies.
    data = dict(aircraft)
    data["capacity"] = (aircraft["cargo_capacity_kg"]
                        if aircraft["load_type"] == "cargo"
                        else aircraft["pax_capacity"])
    return render_template("fleet_form.html", form=data, aircraft=aircraft,
                           images=aircraft_images(db, aircraft_id))


@bp.route("/<int:aircraft_id>")
def detail(aircraft_id):
    """Public-facing airframe detail: basic specs and photo gallery. Open to
    anonymous visitors so the landing-page fleet showcase tiles link through."""
    db = get_db()
    aircraft = db.execute(
        "SELECT * FROM aircraft WHERE id = ?", (aircraft_id,)
    ).fetchone()
    if aircraft is None:
        flash("Aircraft not found.", "error")
        return redirect(url_for("fleet.index"))
    return render_template("fleet_detail.html", aircraft=aircraft,
                           images=aircraft_images(db, aircraft_id))


@bp.route("/image/<int:image_id>")
def image(image_id):
    """Serve an aircraft photo (public, so it shows on the landing page)."""
    row = get_db().execute(
        "SELECT filename FROM aircraft_images WHERE id = ?", (image_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(AIRCRAFT_DIR, row["filename"])


@bp.route("/<int:aircraft_id>/images", methods=("POST",))
@role_required("admin")
def upload_image(aircraft_id):
    db = get_db()
    aircraft = db.execute(
        "SELECT id FROM aircraft WHERE id = ?", (aircraft_id,)
    ).fetchone()
    if aircraft is None:
        flash("Aircraft not found.", "error")
        return redirect(url_for("fleet.index"))

    count = db.execute(
        "SELECT COUNT(*) AS n FROM aircraft_images WHERE aircraft_id = ?",
        (aircraft_id,),
    ).fetchone()["n"]
    if count >= MAX_IMAGES:
        flash(f"An airframe can have at most {MAX_IMAGES} images. Remove one first.",
              "error")
        return redirect(url_for("fleet.edit", aircraft_id=aircraft_id))

    file = request.files.get("image")
    if not file or not file.filename:
        flash("Choose an image to upload.", "error")
        return redirect(url_for("fleet.edit", aircraft_id=aircraft_id))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in IMAGE_EXT:
        flash("Unsupported image type. Use JPG, PNG, WEBP or GIF.", "error")
        return redirect(url_for("fleet.edit", aircraft_id=aircraft_id))

    AIRCRAFT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{aircraft_id}-{secrets.token_hex(8)}.{ext}"
    file.save(str(AIRCRAFT_DIR / filename))
    db.execute(
        "INSERT INTO aircraft_images (aircraft_id, filename, position) VALUES (?, ?, ?)",
        (aircraft_id, filename, count),
    )
    db.commit()
    flash("Image added.", "success")
    return redirect(url_for("fleet.edit", aircraft_id=aircraft_id))


@bp.route("/images/<int:image_id>/delete", methods=("POST",))
@role_required("admin")
def delete_image(image_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM aircraft_images WHERE id = ?", (image_id,)
    ).fetchone()
    if row is None:
        flash("Image not found.", "error")
        return redirect(url_for("fleet.index"))
    path = AIRCRAFT_DIR / row["filename"]
    if path.exists():
        path.unlink()
    db.execute("DELETE FROM aircraft_images WHERE id = ?", (image_id,))
    db.commit()
    flash("Image removed.", "success")
    return redirect(url_for("fleet.edit", aircraft_id=row["aircraft_id"]))


@bp.route("/<int:aircraft_id>/delete", methods=("POST",))
@role_required("admin")
def delete(aircraft_id):
    db = get_db()
    row = db.execute(
        "SELECT registration FROM aircraft WHERE id = ?", (aircraft_id,)
    ).fetchone()
    if row is None:
        flash("Aircraft not found.", "error")
    else:
        # Remove the image files; the rows themselves cascade with the aircraft.
        for img in db.execute(
            "SELECT filename FROM aircraft_images WHERE aircraft_id = ?", (aircraft_id,)
        ).fetchall():
            path = AIRCRAFT_DIR / img["filename"]
            if path.exists():
                path.unlink()
        db.execute("DELETE FROM aircraft WHERE id = ?", (aircraft_id,))
        db.commit()
        flash(f"{row['registration']} removed from the fleet.", "success")
    return redirect(url_for("fleet.index"))
