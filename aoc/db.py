"""SQLite access layer. One file database under data/, created on first run.

Set AOC_DATA_DIR to store the database somewhere else (e.g. a Docker volume).
"""
import os
import sqlite3
from pathlib import Path

from flask import g

DATA_DIR = Path(
    os.environ.get("AOC_DATA_DIR") or Path(__file__).resolve().parent.parent / "data"
)
DB_PATH = DATA_DIR / "aoc.sqlite3"
# Admin-uploaded media (e.g. the landing-page cover) lives in the data dir so it
# persists alongside the database rather than in the shipped static/ folder.
UPLOAD_DIR = DATA_DIR / "uploads"
AIRCRAFT_DIR = UPLOAD_DIR / "aircraft"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Email is optional (admins create test accounts without one). UNIQUE still
    -- holds, and SQLite treats multiple NULLs as distinct so blanks don't clash.
    email         TEXT UNIQUE COLLATE NOCASE,
    callsign      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    -- Optional real name shown alongside the callsign in member management.
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'standard'
                  CHECK (role IN ('admin', 'standard')),
    -- Manual credit an admin may add on top of the flights/minutes a pilot has
    -- actually logged (e.g. hours transferred in from another VA). The logbook
    -- stays the source of truth; these are simply added to the displayed totals.
    adj_flights   INTEGER NOT NULL DEFAULT 0,
    adj_minutes   INTEGER NOT NULL DEFAULT 0,
    -- Per-pilot bearer token used as the smartCARS 3 credential/session. Stays
    -- NULL until a pilot connects smartCARS; SQLite treats multiple NULLs as
    -- distinct so unconnected pilots don't clash on UNIQUE.
    api_key       TEXT UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS aircraft (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    registration      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    icao_type         TEXT NOT NULL,
    variant           TEXT NOT NULL DEFAULT '',
    load_type         TEXT NOT NULL DEFAULT 'pax'
                      CHECK (load_type IN ('pax', 'cargo')),
    pax_capacity      INTEGER NOT NULL DEFAULT 0,
    cargo_capacity_kg INTEGER NOT NULL DEFAULT 0,
    -- Maximum still-air range in nautical miles. Drives which airframes are
    -- eligible for a route (range >= route distance). 0 means "not set" and
    -- places no range limit, so an aircraft stays eligible for any distance.
    range_nm          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'maintenance', 'retired')),
    simbrief_url      TEXT NOT NULL DEFAULT '',
    livery_url        TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS routes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id       TEXT NOT NULL,
    leg           TEXT NOT NULL CHECK (leg IN ('outbound', 'return')),
    number        INTEGER NOT NULL UNIQUE,
    dep_icao      TEXT NOT NULL,
    arr_icao      TEXT NOT NULL,
    aircraft_type TEXT NOT NULL DEFAULT '',
    aircraft_id   INTEGER REFERENCES aircraft(id) ON DELETE SET NULL,
    route_type    TEXT NOT NULL DEFAULT 'pax'
                  CHECK (route_type IN ('pax', 'cargo')),
    dep_time      TEXT NOT NULL DEFAULT '',
    distance_nm   INTEGER,
    duration_min  INTEGER,
    notes         TEXT NOT NULL DEFAULT '',
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- PIREPs snapshot the flight details so a pilot's logbook survives
-- routes or aircraft being deleted later.
CREATE TABLE IF NOT EXISTS pireps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    route_id        INTEGER REFERENCES routes(id) ON DELETE SET NULL,
    aircraft_id     INTEGER REFERENCES aircraft(id) ON DELETE SET NULL,
    flight_no       TEXT NOT NULL,
    callsign        TEXT NOT NULL,
    dep_icao        TEXT NOT NULL,
    arr_icao        TEXT NOT NULL,
    aircraft_label  TEXT NOT NULL DEFAULT '',
    -- 'charter' is retired (no new charters are filed) but kept in the CHECK so
    -- pre-existing charter logbook rows stay valid. 'training' is the Local
    -- Training flight type (same-airport, 9900-9999 block).
    flight_type     TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (flight_type IN ('scheduled', 'charter', 'training')),
    flight_date     TEXT NOT NULL,
    flight_time_min INTEGER NOT NULL,
    remarks         TEXT NOT NULL DEFAULT '',
    -- Acceptance lifecycle. Manually logged flights are born 'accepted' and count
    -- immediately; smartCARS files them 'prefiled' on start, 'pending' on
    -- completion, and an admin moves them to 'accepted'/'rejected'. Stats count
    -- 'accepted' only.
    status          TEXT NOT NULL DEFAULT 'accepted'
                    CHECK (status IN ('prefiled', 'pending', 'accepted', 'rejected')),
    source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'smartcars')),
    -- The smartCARS bid this PIREP was flown from (informational; bids are
    -- cleared once the PIREP is filed).
    bid_id          INTEGER,
    -- ACARS-reported figures, NULL for manual logs.
    landing_rate    INTEGER,
    fuel_used       INTEGER,
    -- Gzipped raw flight-data blob smartCARS submits on completion, kept for audit.
    acars_raw       BLOB,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Aircraft an administrator has approved for a scheduled route. A route with
-- no rows here places no restriction (any active aircraft may fly it).
CREATE TABLE IF NOT EXISTS route_aircraft (
    route_id    INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    PRIMARY KEY (route_id, aircraft_id)
);

-- Up to a handful of photos per airframe, shown on the aircraft detail page
-- and the landing-page fleet showcase. Files live under uploads/aircraft/.
CREATE TABLE IF NOT EXISTS aircraft_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_aircraft_images ON aircraft_images(aircraft_id);

-- Small key/value store for site-wide settings (e.g. the landing-page cover).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Site-wide announcement banners shown under the topbar. Several may be active
-- at once; they render sorted by severity.
CREATE TABLE IF NOT EXISTS notams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info'
               CHECK (level IN ('info', 'warning', 'critical')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Temporary sign-up gate: admins mint single-use invitation codes that a new
-- member must supply to register. A consumed code keeps its row (used_by/used_at)
-- so admins can see who redeemed it.
CREATE TABLE IF NOT EXISTS invite_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at    TEXT
);

-- A pilot's smartCARS flight bid (book -> start -> complete). The AOC schedule
-- (routes) carries no booking state, so smartCARS bookings live here. Route and
-- aircraft are snapshotted (like pireps) so a bid survives later edits; a charter
-- bid has no route_id and carries a pilot-supplied flight number.
CREATE TABLE IF NOT EXISTS bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    route_id    INTEGER REFERENCES routes(id) ON DELETE SET NULL,
    aircraft_id INTEGER REFERENCES aircraft(id) ON DELETE SET NULL,
    flight_no   TEXT NOT NULL,
    dep_icao    TEXT NOT NULL,
    arr_icao    TEXT NOT NULL,
    flight_type TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (flight_type IN ('scheduled', 'charter', 'training')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- In-flight position trail smartCARS pushes during a tracked flight, one row per
-- telemetry tick, tied to the prefiled PIREP. Powers a future live/replay map.
CREATE TABLE IF NOT EXISTS acars_positions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pirep_id  INTEGER NOT NULL REFERENCES pireps(id) ON DELETE CASCADE,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    altitude  INTEGER,
    heading   INTEGER,
    gs        INTEGER,
    phase     TEXT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pireps_user ON pireps(user_id);
CREATE INDEX IF NOT EXISTS idx_routes_pair ON routes(pair_id);
CREATE INDEX IF NOT EXISTS idx_bids_user ON bids(user_id);
CREATE INDEX IF NOT EXISTS idx_acars_pirep ON acars_positions(pirep_id);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete_setting(db, key):
    db.execute("DELETE FROM settings WHERE key = ?", (key,))


def cover_media(db):
    """The current landing-page cover, or None. 'version' is the file mtime so
    templates can cache-bust when the admin replaces the cover in place."""
    filename = get_setting(db, "cover_file")
    if not filename:
        return None
    path = UPLOAD_DIR / filename
    version = int(path.stat().st_mtime) if path.exists() else 0
    return {
        "file": filename,
        "type": get_setting(db, "cover_type", "image"),
        "version": version,
    }


# The admin can set a separate topbar logo for each theme. A missing variant
# falls back to the bundled default mark (static/logo.svg).
LOGO_VARIANTS = ("dark", "light")


def logo_media(db):
    """The admin-uploaded topbar logos as {'dark': {...} | None, 'light': ...}.
    Each present variant carries its filename and mtime version for cache-busting,
    mirroring cover_media."""
    out = {}
    for which in LOGO_VARIANTS:
        filename = get_setting(db, f"logo_{which}_file")
        if not filename:
            out[which] = None
            continue
        path = UPLOAD_DIR / filename
        out[which] = {
            "file": filename,
            "version": int(path.stat().st_mtime) if path.exists() else 0,
        }
    return out


# Default landing-hero copy, used until an admin overrides it on the Site admin
# page. title2 is the optional second heading line.
HERO_DEFAULTS = {
    "title1": "Across the skies",
    "title2": "Connecting the world",
    "subtitle": "Wings of Canada — sharing the passion for flight above the clouds",
}


def hero_text(db):
    """The landing-hero headings and subtitle (admin-editable, with defaults)."""
    return {
        key: get_setting(db, f"hero_{key}", default)
        for key, default in HERO_DEFAULTS.items()
    }


FOOTER_DEFAULT = "Wings of Canada AOC v3.0.1 - All Rights Reserved"


def footer_text(db):
    """The site-wide footer line (admin-editable, with a default)."""
    return get_setting(db, "footer_text", FOOTER_DEFAULT)


# Most urgent first. Used to order the banners on the page.
NOTAM_LEVELS = ("critical", "warning", "info")
_NOTAM_RANK = {level: i for i, level in enumerate(NOTAM_LEVELS)}


def notams(db):
    """All active NOTAM banners, most urgent first (critical → info), then
    newest first within a severity."""
    rows = db.execute("SELECT * FROM notams").fetchall()
    return sorted(
        rows,
        key=lambda r: (_NOTAM_RANK.get(r["level"], len(NOTAM_LEVELS)), -r["id"]),
    )


def add_notam(db, text, level):
    db.execute(
        "INSERT INTO notams (text, level) VALUES (?, ?)",
        (text, level if level in NOTAM_LEVELS else "info"),
    )


def delete_notam(db, notam_id):
    db.execute("DELETE FROM notams WHERE id = ?", (notam_id,))


# Invite codes use an unambiguous alphabet (no 0/O, 1/I) so they are easy to
# read out or type. Two groups of four, e.g. WOC-7KF9-AB3D.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_code_value():
    import secrets
    groups = ["".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(2)]
    return "WOC-" + "-".join(groups)


def create_invite_code(db, created_by):
    """Mint a fresh single-use invitation code and return its value. Retries on
    the (astronomically unlikely) chance of a collision with an existing code."""
    for _ in range(10):
        code = _new_code_value()
        try:
            db.execute(
                "INSERT INTO invite_codes (code, created_by) VALUES (?, ?)",
                (code, created_by),
            )
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not generate a unique invite code.")


def list_invite_codes(db):
    """All invite codes, unused first then most recently used, with the callsign
    of the redeemer (if any) for display."""
    return db.execute(
        """SELECT c.*, u.callsign AS used_by_callsign
           FROM invite_codes c
           LEFT JOIN users u ON u.id = c.used_by
           ORDER BY (c.used_at IS NOT NULL), c.used_at DESC, c.id DESC"""
    ).fetchall()


def find_unused_invite(db, code):
    """An invite row matching this code that has not been redeemed, or None."""
    return db.execute(
        "SELECT * FROM invite_codes WHERE code = ? AND used_by IS NULL", (code,)
    ).fetchone()


def consume_invite_code(db, code_id, user_id):
    """Mark an invite code as redeemed by a freshly created member."""
    db.execute(
        "UPDATE invite_codes SET used_by = ?, used_at = datetime('now') WHERE id = ?",
        (user_id, code_id),
    )


def delete_invite_code(db, code_id):
    """Remove an unused invite code (used ones are kept for the audit trail)."""
    db.execute(
        "DELETE FROM invite_codes WHERE id = ? AND used_by IS NULL", (code_id,)
    )


def aircraft_images(db, aircraft_id):
    """Photos for one airframe, in display order."""
    return db.execute(
        "SELECT * FROM aircraft_images WHERE aircraft_id = ? ORDER BY position, id",
        (aircraft_id,),
    ).fetchall()


def fleet_showcase(db):
    """Non-retired airframes that have at least one photo, with their first
    image id, active first - powers the landing-page fleet showcase, kept in
    sync with the fleet table."""
    return db.execute(
        """SELECT a.*, (
               SELECT i.id FROM aircraft_images i
               WHERE i.aircraft_id = a.id ORDER BY i.position, i.id LIMIT 1
           ) AS image_id
           FROM aircraft a
           WHERE a.status <> 'retired'
             AND EXISTS (SELECT 1 FROM aircraft_images i WHERE i.aircraft_id = a.id)
           ORDER BY CASE a.status WHEN 'active' THEN 0 ELSE 1 END,
                    a.icao_type, a.registration"""
    ).fetchall()


def _migrate(conn):
    """Bring older databases up to the current schema in place."""
    # The visitor tier was removed (anonymous visitors see the public home
    # page instead); lift any legacy accounts to standard.
    conn.execute("UPDATE users SET role = 'standard' WHERE role = 'visitor'")

    # Admin-set credit on top of logged flights/hours (transferred experience,
    # corrections). Added after launch, so backfill older databases.
    user_info = {r["name"]: r for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "adj_flights" not in user_info:
        conn.execute("ALTER TABLE users ADD COLUMN adj_flights INTEGER NOT NULL DEFAULT 0")
    if "adj_minutes" not in user_info:
        conn.execute("ALTER TABLE users ADD COLUMN adj_minutes INTEGER NOT NULL DEFAULT 0")

    # Optional real name, added after launch for the member-management roster.
    if "name" not in user_info:
        conn.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")

    # Email became optional. The original table baked in NOT NULL, which SQLite
    # cannot drop via ALTER, so rebuild the table once (ids preserved, so the
    # pireps/routes foreign keys still line up). Runs after the adj_* columns
    # above so the SELECT below can copy them.
    if user_info.get("email") is not None and user_info["email"]["notnull"] == 1:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE users_rebuild (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE COLLATE NOCASE,
                callsign      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name          TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'standard'
                              CHECK (role IN ('admin', 'standard')),
                adj_flights   INTEGER NOT NULL DEFAULT 0,
                adj_minutes   INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO users_rebuild
                SELECT id, email, callsign, name, password_hash, role,
                       adj_flights, adj_minutes, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_rebuild RENAME TO users;
            PRAGMA foreign_keys=ON;
            """
        )

    # load_type (pax/cargo) was added after launch. Backfill existing rows:
    # an aircraft with cargo capacity but no seats is treated as a freighter.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(aircraft)").fetchall()]
    if "load_type" not in cols:
        conn.execute(
            "ALTER TABLE aircraft ADD COLUMN load_type TEXT NOT NULL DEFAULT 'pax'"
        )
        conn.execute(
            """UPDATE aircraft SET load_type = 'cargo'
               WHERE cargo_capacity_kg > 0 AND pax_capacity = 0"""
        )

    # Routes now reference a specific airframe (so the network can show the
    # registration and variant), not just the ICAO type. Older databases only
    # have aircraft_type; add the link column and best-effort backfill it by
    # matching the stored type to an aircraft of that type.
    route_cols = [r["name"] for r in conn.execute("PRAGMA table_info(routes)").fetchall()]
    if "aircraft_id" not in route_cols:
        conn.execute("ALTER TABLE routes ADD COLUMN aircraft_id INTEGER REFERENCES aircraft(id)")
        conn.execute(
            """UPDATE routes SET aircraft_id = (
                   SELECT a.id FROM aircraft a
                   WHERE a.icao_type = routes.aircraft_type
                   ORDER BY a.id LIMIT 1)
               WHERE aircraft_type <> ''"""
        )

    # 'charter' was added as a third load type. The original aircraft table
    # baked CHECK (load_type IN ('pax','cargo')) into its definition, which
    # SQLite cannot widen with ALTER, so rebuild the table (ids preserved, so
    # routes/pireps foreign keys still line up).
    ac = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='aircraft'"
    ).fetchone()
    if ac and "charter" not in ac["sql"]:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE aircraft_rebuild (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                registration      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                icao_type         TEXT NOT NULL,
                variant           TEXT NOT NULL DEFAULT '',
                load_type         TEXT NOT NULL DEFAULT 'pax'
                                  CHECK (load_type IN ('pax', 'cargo', 'charter')),
                pax_capacity      INTEGER NOT NULL DEFAULT 0,
                cargo_capacity_kg INTEGER NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'maintenance', 'retired')),
                simbrief_url      TEXT NOT NULL DEFAULT '',
                livery_url        TEXT NOT NULL DEFAULT '',
                notes             TEXT NOT NULL DEFAULT '',
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO aircraft_rebuild
                SELECT id, registration, icao_type, variant, load_type,
                       pax_capacity, cargo_capacity_kg, status, simbrief_url,
                       livery_url, notes, created_at FROM aircraft;
            DROP TABLE aircraft;
            ALTER TABLE aircraft_rebuild RENAME TO aircraft;
            PRAGMA foreign_keys=ON;
            """
        )

    # Aircraft now carry a maximum range (nm). Route eligibility is computed from
    # range vs the route distance, so older rows backfill to 0 ("not set" => no
    # range limit). Re-query columns: the block above may have rebuilt the table.
    ac_cols = [r["name"] for r in conn.execute("PRAGMA table_info(aircraft)").fetchall()]
    if "range_nm" not in ac_cols:
        conn.execute("ALTER TABLE aircraft ADD COLUMN range_nm INTEGER NOT NULL DEFAULT 0")

    # Charter generation was retired. Charter airframes carried passengers, so
    # fold any that exist into the PAX category (their CHECK still admits the old
    # value, so no rebuild is needed to convert the rows).
    conn.execute("UPDATE aircraft SET load_type = 'pax' WHERE load_type = 'charter'")

    # Local Training flights reuse the freed 9900-9999 block and are stored with
    # flight_type='training'. The bids table baked CHECK (flight_type IN
    # ('scheduled','charter')) into its definition, which SQLite cannot widen with
    # ALTER, so rebuild it once to admit 'training' (ids preserved; the
    # informational pireps.bid_id still lines up). pireps needs no rebuild — its
    # flight_type column was added by ALTER and carries no CHECK.
    bd = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()
    if bd and "training" not in bd["sql"]:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE bids_rebuild (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_id    INTEGER REFERENCES routes(id) ON DELETE SET NULL,
                aircraft_id INTEGER REFERENCES aircraft(id) ON DELETE SET NULL,
                flight_no   TEXT NOT NULL,
                dep_icao    TEXT NOT NULL,
                arr_icao    TEXT NOT NULL,
                flight_type TEXT NOT NULL DEFAULT 'scheduled'
                            CHECK (flight_type IN ('scheduled', 'charter', 'training')),
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO bids_rebuild
                SELECT id, user_id, route_id, aircraft_id, flight_no, dep_icao,
                       arr_icao, flight_type, created_at FROM bids;
            DROP TABLE bids;
            ALTER TABLE bids_rebuild RENAME TO bids;
            CREATE INDEX IF NOT EXISTS idx_bids_user ON bids(user_id);
            PRAGMA foreign_keys=ON;
            """
        )

    # Scheduled flights now carry a set of admin-approved aircraft (route_aircraft,
    # created by the schema above). Seed it once from the single aircraft_id a
    # route used to carry, so existing routes keep their aircraft as approved.
    if conn.execute("SELECT COUNT(*) FROM route_aircraft").fetchone()[0] == 0:
        conn.execute(
            """INSERT OR IGNORE INTO route_aircraft (route_id, aircraft_id)
               SELECT id, aircraft_id FROM routes WHERE aircraft_id IS NOT NULL"""
        )

    # Flights are now Scheduled or Charter; older PIREPs predate the column.
    pirep_cols = [r["name"] for r in conn.execute("PRAGMA table_info(pireps)").fetchall()]
    if "flight_type" not in pirep_cols:
        conn.execute(
            "ALTER TABLE pireps ADD COLUMN flight_type TEXT NOT NULL DEFAULT 'scheduled'"
        )

    # Scheduled routes gained a PAX/Cargo type and a scheduled departure time.
    route_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(routes)").fetchall()]
    if "route_type" not in route_cols2:
        conn.execute(
            "ALTER TABLE routes ADD COLUMN route_type TEXT NOT NULL DEFAULT 'pax'"
        )
    if "dep_time" not in route_cols2:
        conn.execute("ALTER TABLE routes ADD COLUMN dep_time TEXT NOT NULL DEFAULT ''")

    # NOTAM banners moved from a single settings pair to their own table (so
    # several can be active at once). Carry over any existing single banner once.
    old = conn.execute(
        "SELECT value FROM settings WHERE key = 'notam_text'"
    ).fetchone()
    if old is not None:
        text = (old["value"] or "").strip()
        if text:
            level = conn.execute(
                "SELECT value FROM settings WHERE key = 'notam_level'"
            ).fetchone()
            level = level["value"] if level else "info"
            if level not in ("info", "warning", "critical"):
                level = "info"
            conn.execute(
                "INSERT INTO notams (text, level) VALUES (?, ?)", (text, level)
            )
        conn.execute("DELETE FROM settings WHERE key IN ('notam_text', 'notam_level')")

    # smartCARS 3 integration (see SMARTCARS-INTEGRATION.md). All additive, so
    # existing logbooks and stats are unchanged. The bids / acars_positions tables
    # are created by the schema above; these blocks patch the older users / pireps
    # tables in place. Re-query table_info here (rather than reuse the snapshot
    # near the top) so it reflects any rebuild done above.
    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "api_key" not in user_cols:
        # SQLite can't add a UNIQUE column via ALTER, so add it plain and enforce
        # uniqueness with an index (multiple NULLs stay distinct, so pilots who
        # haven't connected smartCARS don't collide). Fresh DBs already carry the
        # column-level UNIQUE from the schema, so this only runs when upgrading.
        conn.execute("ALTER TABLE users ADD COLUMN api_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)"
        )

    # PIREPs gained an acceptance lifecycle and ACARS fields. Existing rows
    # default to accepted/manual, so nothing about the current logbook changes;
    # only smartCARS-filed PIREPs land pending and await admin acceptance.
    pirep_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(pireps)").fetchall()]
    if "status" not in pirep_cols2:
        conn.execute(
            "ALTER TABLE pireps ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted' "
            "CHECK (status IN ('prefiled', 'pending', 'accepted', 'rejected'))"
        )
    if "source" not in pirep_cols2:
        conn.execute(
            "ALTER TABLE pireps ADD COLUMN source TEXT NOT NULL DEFAULT 'manual' "
            "CHECK (source IN ('manual', 'smartcars'))"
        )
    if "bid_id" not in pirep_cols2:
        conn.execute("ALTER TABLE pireps ADD COLUMN bid_id INTEGER")
    if "landing_rate" not in pirep_cols2:
        conn.execute("ALTER TABLE pireps ADD COLUMN landing_rate INTEGER")
    if "fuel_used" not in pirep_cols2:
        conn.execute("ALTER TABLE pireps ADD COLUMN fuel_used INTEGER")
    if "acars_raw" not in pirep_cols2:
        conn.execute("ALTER TABLE pireps ADD COLUMN acars_raw BLOB")


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_db()
