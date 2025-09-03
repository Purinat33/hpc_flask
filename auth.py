# auth.py
from functools import wraps
from flask import Blueprint, render_template_string, request, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import os
from ui_base import nav as render_nav
auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()
login_manager.login_view = "auth.login"

# --- Dummy user store (replace with real PAM/LDAP/OIDC) ---
USERS = {
    # username: {password, role}
    "admin": {"password": os.environ.get("ADMIN_PASSWORD", "admin123"), "role": "admin"},
    "alice": {"password": "alice", "role": "user"},
    "bob":   {"password": "bob",   "role": "user"},
    "akara.sup":   {"password": "12345",   "role": "user"},
    "surapol.gits":   {"password": "12345",   "role": "user"},
}


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
    info = USERS.get(user_id)
    if not info:
        return None
    return User(user_id, info["role"])

# Decorator for admin-only routes


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            flash("You need admin permissions to access that page.")
            # or url_for("user.my_usage")
            return redirect(url_for("playground"))
        return f(*args, **kwargs)
    return wrapper


PAGE = """
<!doctype html><title>Login</title>
<style>body{font-family:system-ui,Arial;margin:2rem}form{max-width:360px}input{width:100%;padding:.6rem;margin:.25rem 0;border:1px solid #bbb;border-radius:8px}button{padding:.6rem 1rem;border:0;border-radius:8px;background:#1f7aec;color:#fff}</style>
{{ NAV|safe }}
<h2>Sign in</h2>
{% with msgs = get_flashed_messages() %}
  {% if msgs %}{% for m in msgs %}<div>{{m}}</div>{% endfor %}{% endif %}
{% endwith %}
<form method="post">
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
    row = USERS.get(u)
    if not row or row["password"] != p:
        flash("Invalid username or password")
        return redirect(url_for("auth.login"))
    login_user(User(u, row["role"]))  # starts a session cookie
    flash(f"Welcome, {u}!")
    return redirect(request.args.get("next") or url_for("playground"))


@auth_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out")
    return redirect(url_for("auth.login"))
