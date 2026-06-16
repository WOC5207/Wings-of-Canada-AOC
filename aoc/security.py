"""Login/permission helpers. Role hierarchy: standard < admin.

Anonymous visitors are served by the public home page; every registered
member is at least Standard.
"""
from functools import wraps

from flask import flash, g, redirect, request, session, url_for

from .db import get_db

ROLE_RANK = {"standard": 1, "admin": 2}
ROLE_LABELS = {"standard": "Standard", "admin": "Administrator"}


def _rank(role):
    return ROLE_RANK.get(role, 0)


def load_current_user():
    """Populate g.user from the session at the start of every request."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please sign in to access the Operations Centre.", "error")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(min_role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Please sign in to access the Operations Centre.", "error")
                return redirect(url_for("auth.login", next=request.path))
            if _rank(g.user["role"]) < ROLE_RANK[min_role]:
                flash(
                    f"That action requires {ROLE_LABELS[min_role]} access. "
                    "Contact an administrator if you need your tier upgraded.",
                    "error",
                )
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
