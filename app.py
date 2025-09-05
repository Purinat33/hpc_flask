# app.py
import os
from flask import Flask, render_template, request, jsonify, abort, redirect, url_for
from flask_login import login_required, current_user
from models.rates_store import load_rates, save_rates
from controllers.admin import admin_bp
from controllers.auth import auth_bp, login_manager, admin_required
from controllers.user import user_bp
from models.db import init_app as init_db_app, init_db
from controllers.api import api_bp
from models import rates_store
from models.users_db import init_app as init_users_app, init_users_db, get_user, create_user
import logging
from logging.handlers import RotatingFileHandler
from flask import g, request
from time import time
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf
from flask_babel import Babel, gettext as _, get_locale
from flask import redirect, request, url_for, abort, current_app, make_response
from models.audit_store import init_audit_schema
from models.security_throttle import init_throttle_schema  # ⭐ NEW

babel = Babel()


def select_locale():
    from flask import request, current_app
    langs = current_app.config.get("LANGUAGES", ["en", "th"])
    cookie = request.cookies.get("lang")
    if cookie in langs:
        return cookie
    return request.accept_languages.best_match(langs) or "en"


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    # Babel Setup
    app.config.setdefault("BABEL_DEFAULT_LOCALE", "en")
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", "translations")
    app.config.setdefault("LANGUAGES", ["en", "th"])

    babel.init_app(app, locale_selector=select_locale)

    # make helpers available in Jinja
    app.jinja_env.globals["_"] = _
    app.jinja_env.globals["get_locale"] = get_locale
    app.jinja_env.globals["locale_code"] = lambda: str(get_locale())

    # CSRF Setup
    csrf = CSRFProtect()
    csrf.init_app(app)
    app.jinja_env.globals["csrf_token"] = generate_csrf

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        app.logger.warning("CSRF failed: %s", getattr(e, "description", ""))
        return render_template("errors/csrf.html", reason=getattr(e, "description", "")), 400

    # --- Logging setup ---
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, "app.log")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    # --- / Logging Setup --- #

    os.makedirs(app.instance_path, exist_ok=True)

    # Config (env overrides)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or "dev-secret",
        BILLING_DB=os.environ.get("BILLING_DB") or os.path.join(
            app.instance_path, "billing.sqlite3"),
        FALLBACK_CSV=os.environ.get("FALLBACK_CSV") or os.path.join(
            app.instance_path, "test.csv"),
        # throttle knobs (tweak via env vars)
        AUTH_THROTTLE_MAX_FAILS=int(
            os.environ.get("AUTH_THROTTLE_MAX_FAILS", "5")),
        AUTH_THROTTLE_WINDOW_SEC=int(
            os.environ.get("AUTH_THROTTLE_WINDOW_SEC", "60")),
        AUTH_THROTTLE_LOCK_SEC=int(os.environ.get(
            "AUTH_THROTTLE_LOCK_SEC", "300")),
    )

    # DB & login
    init_db_app(app)
    init_users_app(app)
    with app.app_context():
        init_db()
        init_users_db()
        init_audit_schema()
        init_throttle_schema()  #  NEW
        # Seed admin (idempotent)
        admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
        if not get_user("admin"):
            create_user("admin", admin_pwd, role="admin")

        if os.environ.get("SEED_DEMO_USERS") == "1":
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

    @app.errorhandler(404)
    def not_found(e):
        app.logger.warning("404 %s %s", request.method, request.path)
        return render_template("errors/404.html", path=request.path), 404

    @app.errorhandler(405)
    def not_allowed(e):
        app.logger.warning("405 %s %s", request.method, request.path)
        return render_template("errors/404.html", path=request.path), 405

    @app.get("/")
    def root():
        return redirect(url_for("playground"))

    @app.get("/playground")
    def playground():
        return render_template("playground.html")

    from flask import g
    from time import time

    @app.before_request
    def _start_timer():
        g._t0 = time()

    @app.after_request
    def _log_request(resp):
        try:
            ms = (time() - getattr(g, "_t0", time())) * 1000
            app.logger.info("%s %s %s %s %.1fms",
                            request.remote_addr, request.method, request.full_path, resp.status_code, ms)
        except Exception:
            app.logger.exception("Failed to log request")
        return resp

    @app.post("/i18n/set")
    def set_locale():
        lang = request.form.get("lang", "en")
        if lang not in current_app.config["LANGUAGES"]:
            abort(400)
        resp = make_response(
            redirect(request.referrer or url_for("playground")))
        resp.set_cookie("lang", lang, max_age=60 *
                        60 * 24 * 365, samesite="Lax")
        return resp

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
