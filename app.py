# app.py
import os
from flask import Flask, render_template, request, jsonify, render_template_string, abort, redirect, url_for
from flask_login import login_required, current_user
from models.rates_store import load_rates, save_rates
from controllers.admin import admin_bp
from controllers.auth import auth_bp, login_manager, admin_required
from controllers.user import user_bp
from services.ui_base import nav as render_nav
from models.db import init_app as init_db_app, init_db
from controllers.api import api_bp
from models import rates_store
from models.users_db import init_app as init_users_app, init_users_db, get_user, create_user
import logging
from logging.handlers import RotatingFileHandler
from flask import g, request
from time import time
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    # CSRF Setup
    csrf = CSRFProtect()

    csrf.init_app(app)

    # Make {{ csrf_token() }} available in all Jinja templates
    app.jinja_env.globals["csrf_token"] = generate_csrf

    # Optional: nicer error if token missing/invalid

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        app.logger.warning("CSRF failed: %s", getattr(e, "description", ""))
        return render_template("errors/csrf.html", reason=getattr(e, "description", "")), 400

    # --- Logging setup ---
    log_dir = os.path.join(os.path.dirname(__file__),
                           "log")   # project-root/log
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, "app.log")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Set levels and attach handler only once (avoid duplicates under reloader)
    root_logger = logging.getLogger()          # captures everything
    root_logger.setLevel(logging.INFO)

    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)

    # Flask's app logger inherits root settings; ensure level is not lower
    app.logger.setLevel(logging.INFO)

    # --- / Logging Setup --- #
    os.makedirs(app.instance_path, exist_ok=True)

    # Set config (env overrides, else defaults)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or "dev-secret",
        BILLING_DB=os.environ.get("BILLING_DB")
        or os.path.join(app.instance_path, "billing.sqlite3"),
        FALLBACK_CSV=os.environ.get("FALLBACK_CSV")
        or os.path.join(app.instance_path, "test.csv"),
    )

    # DB & login
    init_db_app(app)
    init_users_app(app)
    with app.app_context():
        init_db()
        init_users_db()
        # Seed admin (idempotent)
        admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
        if not get_user("admin"):
            create_user("admin", admin_pwd, role="admin")

        # (Optional) seed a few demo users; remove in prod
        demo = {
            "alice": ("alice", "user"),
            "bob": ("bob", "user"),
            "akara.sup": ("12345", "user"),
            "surapol.gits": ("12345", "user"),
        }
        for u, (pwd, role) in demo.items():
            if not get_user(u):
                create_user(u, pwd, role)

    login_manager.init_app(app)

    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(api_bp)

    # Specifically error routes
    @app.errorhandler(404)
    def not_found(e):
        # optional: log it
        app.logger.warning("404 %s %s", request.method, request.path)
        return render_template("errors/404.html",
                               NAV=render_nav("home"),
                               path=request.path), 404

    @app.errorhandler(405)
    def not_found(e):
        # Just gonna reuse the same thing
        app.logger.warning("405 %s %s", request.method, request.path)
        return render_template("errors/404.html",
                               NAV=render_nav("home"),
                               path=request.path), 405

    # Routes that render templates
    @app.get("/")
    def root():
        return redirect(url_for("playground"))

    @app.get("/playground")
    def playground():
        return render_template("playground.html", NAV=render_nav("home"))

    @app.before_request
    def _start_timer():
        g._t0 = time()

    @app.after_request
    def _log_request(resp):
        try:
            ms = (time() - getattr(g, "_t0", time())) * 1000
            app.logger.info("%s %s %s %s %.1fms",
                            request.remote_addr,
                            request.method,
                            request.full_path,
                            resp.status_code,
                            ms)
        except Exception:
            app.logger.exception("Failed to log request")
        return resp

    return app


# Keep a module-level `app` so `flask --app app run` works
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
