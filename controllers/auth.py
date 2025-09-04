# auth.py
from functools import wraps
from flask import Blueprint, render_template_string, request, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import os
from services.ui_base import nav as render_nav
from models.users_db import get_user, verify_password

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


PAGE = """
<!doctype html><title>Login</title>
<style>body{font-family:system-ui,Arial;margin:2rem}form{max-width:360px}input{width:100%;padding:.6rem;margin:.25rem 0;border:1px solid #bbb;border-radius:8px}button{padding:.6rem 1rem;border:0;border-radius:8px;background:#1f7aec;color:#fff}</style>
{{ NAV|safe }}
<h2>Sign in</h2>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input name="username" placeholder="username" required>
  <input name="password" type="password" placeholder="password" required>
  <button type="submit">Login</button>
</form>
"""


@auth_bp.get("/login")
def login():
    return render_template_string(PAGE, NAV=render_nav("home"))


@auth_bp.post("/login")
def login_post():
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "")
    if not verify_password(u, p):
        return redirect(url_for("auth.login"))
    row = get_user(u)
    login_user(User(row["username"], row["role"]))
    return redirect(request.args.get("next") or url_for("playground"))


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    # flash("Signed out")
    return redirect(url_for("auth.login"))
