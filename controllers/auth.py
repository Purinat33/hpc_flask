from functools import wraps
from flask import Blueprint, flash, render_template, request, redirect, url_for, abort, current_app
from flask_login import LoginManager, UserMixin, confirm_login, fresh_login_required, login_user, logout_user, login_required, current_user
from models.users_db import get_user, update_password, verify_password
from models.audit_store import audit
from models.security_throttle import is_locked, register_failure, reset, get_status
from services.metrics import (
    LOGIN_SUCCESSES, LOGIN_FAILURES,
    LOCKOUT_ACTIVE, LOCKOUT_START, LOCKOUT_END,
    FORBIDDEN_REDIRECTS,
)

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()
login_manager.login_view = "auth.login"


class User(UserMixin):
    def __init__(self, username, role):
        self.id = username
        self.username = username
        self.role = role

    @property
    def is_admin(self):
        return self.role == "admin"


@login_manager.user_loader
def load_user(user_id):
    row = get_user(user_id)
    if not row:
        return None
    return User(row["username"], row["role"])


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            FORBIDDEN_REDIRECTS.inc()
            audit(
                "auth.forbidden",
                target_type="user", target_id=(getattr(current_user, "username", "") or "anonymous"),
                outcome="failure", status=403,
                extra={"reason": "not_admin"}
            )
            return redirect(url_for("playground"))
        return f(*args, **kwargs)
    return wrapper


@auth_bp.get("/login")
def login():
    # Build a quick inline message (no flash) from query params
    err = (request.args.get("err") or "").lower()
    left = request.args.get("left", type=int)
    message = ""

    if err == "locked":
        secs = max(left or 0, 0)
        mm = secs // 60
        ss = secs % 60
        # keep this short and neutral
        message = f"Too many failed attempts. Try again in {mm}:{ss:02d}."
    elif err == "bad":
        message = "Invalid username or password."

    # (Optional) repopulate username for UX if provided
    username = request.args.get("u", "")

    return render_template("auth/login.html", message=message, last_username=username)


@auth_bp.post("/login")
def login_post():
    u = (request.form.get("username") or "").strip()
    p = request.form.get("password", "")
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")

    # If currently locked, audit & bounce with message
    locked, sec_left = is_locked(u or "unknown", ip)
    if locked:
        LOCKOUT_ACTIVE.inc()
        audit(
            "auth.lockout.active",
            target_type="user", target_id=(u or "unknown"),
            outcome="failure", status=423,
            extra={"note": f"seconds_left={sec_left}"}
        )
        return redirect(url_for("auth.login", err="locked", left=sec_left, u=u))

    # Check password
    if not verify_password(u, p):
        LOGIN_FAILURES.labels(reason="bad_credentials").inc()
        audit(
            "auth.login.failure",
            target_type="user", target_id=(u or "unknown"),
            outcome="failure", status=401,
            error_code="bad_credentials",
            extra={"reason": "bad_credentials"}
        )
        locked_now = register_failure(u or "unknown", ip)
        if locked_now:
            # best-effort seconds-left (new full lock window)
            LOCKOUT_START.inc()
            lock_sec = int(current_app.config.get(
                "AUTH_THROTTLE_LOCK_SEC", 300))
            audit(
                "auth.lockout.start",
                target_type="user", target_id=(u or "unknown"),
                outcome="failure", status=423,
                extra={"note": f"lock_sec={lock_sec}"}
            )
            return redirect(url_for("auth.login", err="locked", left=lock_sec, u=u))

        # generic invalid-credentials message
        return redirect(url_for("auth.login", err="bad", u=u))

    # Success: clear throttle and log in
    prev = get_status(u or "unknown", ip)
    was_locked = bool(prev.get("locked_until"))
    reset(u or "unknown", ip)

    row = get_user(u)
    login_user(User(row["username"], row["role"]))
    LOGIN_SUCCESSES.inc()

    audit(
        "auth.login.success",
        target_type="user", target_id=row["username"],
        outcome="success", status=200,
        extra={"note": f"role={row['role']}"}
    )
    if was_locked:
        LOCKOUT_END.inc()
        audit(
            "auth.lockout.end",
            target_type="user", target_id=row["username"],
            outcome="success", status=200
        )

    # Always land on home; ignore ?next=
    return redirect(url_for("playground"))


@auth_bp.post("/logout")
@login_required
def logout():
    audit(
        "auth.logout",
        target_type="user", target_id=current_user.username,
        outcome="success", status=200
    )
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.get("/account/password")
@login_required
def change_password_form():
    # no sidebar here; this page is for all users
    return render_template("account/password.html")


@auth_bp.post("/account/password")
@login_required
@fresh_login_required
def change_password_submit():
    old = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    new2 = request.form.get("new_password2") or ""
    uname = getattr(current_user, "username", None)

    # Basic validations
    if not uname:
        flash("Not authenticated.", "error")
        return redirect(url_for("auth.change_password_form"))
    if not old or not new or not new2:
        flash("Please fill in all fields.", "error")
        return redirect(url_for("auth.change_password_form"))
    if new != new2:
        flash("New passwords do not match.", "error")
        return redirect(url_for("auth.change_password_form"))
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("auth.change_password_form"))
    if new == old:
        flash("New password must be different from the current password.", "error")
        return redirect(url_for("auth.change_password_form"))

    ok = verify_password(uname, old)
    if not ok:
        audit("user.password.change", target_type="user", target_id=uname,
              outcome="failure", status=400, extra={"reason": "bad_current"})
        flash("Current password is incorrect.", "error")
        return redirect(url_for("auth.change_password_form"))

    try:
        update_password(uname, new)
        # Force logout
        audit("user.password.change", target_type="user", target_id=uname,
              outcome="success", status=200)
        logout_user()
        return redirect(url_for("auth.login"))
    except Exception as e:
        audit("user.password.change", target_type="user", target_id=uname,
              outcome="failure", status=500, extra={"error": str(e)})
        flash("Could not update password. Please try again.", "error")
        return redirect(url_for("auth.change_password_form"))
