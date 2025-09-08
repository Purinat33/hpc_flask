from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, abort, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from models.users_db import get_user, verify_password
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
            audit(action="auth.forbidden",
                  target=f"user={current_user.username}",
                  status=403,
                  extra={"path": request.path})
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
        audit(action="auth.lockout.active",
              target=f"user={u or 'unknown'}",
              status=423,
              extra={"ip": ip, "seconds_left": sec_left})
        return redirect(url_for("auth.login", err="locked", left=sec_left, u=u))

    # Check password
    if not verify_password(u, p):
        LOGIN_FAILURES.labels(reason="bad_credentials").inc()
        audit(action="auth.login.failure",
              target=f"user={u or 'unknown'}",
              status=401,
              extra={"reason": "bad_credentials", "ip": ip, "ua": request.headers.get("User-Agent")})

        locked_now = register_failure(u or "unknown", ip)
        if locked_now:
            # best-effort seconds-left (new full lock window)
            LOCKOUT_START.inc()
            lock_sec = int(current_app.config.get(
                "AUTH_THROTTLE_LOCK_SEC", 300))
            audit(action="auth.lockout.start",
                  target=f"user={u or 'unknown'}",
                  status=423,
                  extra={
                      "ip": ip,
                      "window_sec": current_app.config.get("AUTH_THROTTLE_WINDOW_SEC", 60),
                      "max_fails": current_app.config.get("AUTH_THROTTLE_MAX_FAILS", 5),
                      "lock_sec": lock_sec,
                  })
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

    audit(action="auth.login.success",
          target=f"user={row['username']}",
          status=200,
          extra={"role": row["role"], "ip": ip, "ua": request.headers.get("User-Agent")})

    if was_locked:
        LOCKOUT_END.inc()
        audit(action="auth.lockout.end",
              target=f"user={row['username']}",
              status=200,
              extra={"ip": ip})

    # Always land on home; ignore ?next=
    return redirect(url_for("playground"))


@auth_bp.post("/logout")
@login_required
def logout():
    audit("auth.logout", target=f"user={current_user.username}", status=200)
    logout_user()
    return redirect(url_for("auth.login"))
