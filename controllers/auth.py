# auth.py
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import os
from models.users_db import get_user, verify_password
from models.audit_store import audit

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

# Decorator for admin-only routes


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            # flash("You need admin permissions to access that page.")
            # or url_for("user.my_usage")
            return redirect(url_for("playground"))
        return f(*args, **kwargs)
    return wrapper


@auth_bp.get("/login")
def login():
    return render_template("auth/login.html")


# auth.py
@auth_bp.post("/login")
def login_post():
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "")
    if not verify_password(u, p):
        audit(action="auth.login.failure",
              target=f"user={u or 'unknown'}",
              status=401,
              extra={"reason": "bad_credentials", "ip": request.remote_addr, "ua": request.headers.get("User-Agent")})
        return redirect(url_for("auth.login"))

    row = get_user(u)
    login_user(User(row["username"], row["role"]))

    # Always land on home; ignore ?next=
    audit(action="auth.login.success",
          target=f"user={row['username']}",
          status=200,
          extra={"role": row["role"], "ip": request.remote_addr, "ua": request.headers.get("User-Agent")})
    return redirect(url_for("playground"))


@auth_bp.post("/logout")
@login_required
def logout():
    audit("auth.logout", target=f"user={current_user.username}", status=200)
    logout_user()
    # flash("Signed out")
    return redirect(url_for("auth.login"))
