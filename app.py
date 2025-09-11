from models.base import init_engine_and_session, Base
import os
import logging
from logging.handlers import RotatingFileHandler
from time import time

from flask import Flask, render_template, request, redirect, url_for, abort, current_app, make_response, g
from flask_babel import Babel, gettext as _, get_locale
from flask_login import login_required, current_user
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf
from dotenv import load_dotenv

from controllers.admin import admin_bp
from controllers.api import api_bp
from controllers.auth import auth_bp, login_manager, admin_required
from controllers.user import user_bp
from controllers.payments import payments_bp
from controllers.payments import webhook as payments_webhook
from services.metrics import init_app as init_metrics, REQUEST_COUNT, REQUEST_LATENCY

babel = Babel()

# --- Load .env exactly once, here ---
# If you run "python app.py", this ensures variables are loaded.
# If you use "flask run", Flask will also load .env automatically (when python-dotenv is installed).
load_dotenv()


def select_locale():
    langs = current_app.config.get("LANGUAGES", ["en", "th"])
    cookie = request.cookies.get("lang")
    if cookie in langs:
        return cookie
    return request.accept_languages.best_match(langs) or "en"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _parse_demo_users(env_val: str) -> dict[str, tuple[str, str]]:
    """
    Parse DEMO_USERS in .env like:
      "alice:alice:user,bob:bob:user,akara.sup:12345:user,surapol.gits:12345:user"
    Returns {username: (password, role)}; role defaults to "user" if omitted.
    Invalid entries are ignored.
    """
    out: dict[str, tuple[str, str]] = {}
    if not env_val:
        return out
    for item in env_val.split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 3:
            u, pwd, role = parts
        elif len(parts) == 2:
            u, pwd = parts
            role = "user"
        else:
            continue
        if u and pwd:
            out[u] = (pwd, role or "user")
    return out


def create_app(test_config: dict | None = None):
    app = Flask(__name__, instance_relative_config=True)

    # ---- Base config from environment (no hardcoded secrets) ----
    APP_ENV = os.getenv("APP_ENV", "development").lower()

    # SECRET_KEY:
    # - In production: must be provided
    # - In dev: fall back to a random key each run (sessions will reset on restart)
    secret_key = os.getenv("FLASK_SECRET_KEY")
    if not secret_key and APP_ENV == "production":
        raise RuntimeError("FLASK_SECRET_KEY must be set in production (.env)")
    if not secret_key:
        secret_key = os.urandom(32)  # dev-only fallback

    app.config.from_mapping(
        SECRET_KEY=secret_key,
        APP_ENV=APP_ENV,

        # Databases & files
        FALLBACK_CSV=os.getenv("FALLBACK_CSV") or os.path.join(
            app.instance_path, "test.csv"),

        # Auth throttling knobs
        AUTH_THROTTLE_MAX_FAILS=int(os.getenv("AUTH_THROTTLE_MAX_FAILS", "5")),
        AUTH_THROTTLE_WINDOW_SEC=int(
            os.getenv("AUTH_THROTTLE_WINDOW_SEC", "60")),
        AUTH_THROTTLE_LOCK_SEC=int(os.getenv("AUTH_THROTTLE_LOCK_SEC", "300")),

        # i18n
        BABEL_DEFAULT_LOCALE="en",
        BABEL_TRANSLATION_DIRECTORIES="translations",
        LANGUAGES=["en", "th"],
    )
    if test_config:
        app.config.update(test_config)

    # ---- Babel ----
    babel.init_app(app, locale_selector=select_locale)
    app.jinja_env.globals["_"] = _
    app.jinja_env.globals["get_locale"] = get_locale
    app.jinja_env.globals["locale_code"] = lambda: str(get_locale())

    # ---- CSRF ----
    csrf = CSRFProtect()
    csrf.init_app(app)
    csrf.exempt(payments_webhook)
    app.jinja_env.globals["csrf_token"] = generate_csrf

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        app.logger.warning("CSRF failed: %s", getattr(e, "description", ""))
        return render_template("errors/csrf.html", reason=getattr(e, "description", "")), 400

    # ---- Logging ----
    log_dir = os.path.join(os.path.dirname(__file__), "log")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "app.log")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    file_handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)

    # Ensure instance folder exists
    os.makedirs(app.instance_path, exist_ok=True)

    # ---- DB & users init ----
    from models.users_db import get_user, create_user
    engine, _Session = init_engine_and_session()

    if os.getenv("AUTO_CREATE_SCHEMA", "1") in ("1", "true", "yes", "on"):
        Base.metadata.create_all(engine, checkfirst=True)

    with app.app_context():
        # seed admin (optional)
        admin_pwd = os.getenv("ADMIN_PASSWORD")
        if admin_pwd and not get_user("admin"):
            create_user("admin", admin_pwd, role="admin")
            app.logger.info("Seeded admin user from .env")

        # seed demo users in dev (optional)
        if app.config["APP_ENV"] == "development" and _env_bool("SEED_DEMO_USERS", True):
            demo_env = os.getenv("DEMO_USERS", "")
            demo = _parse_demo_users(demo_env) or {
                "alice": ("alice", "user"),
                "bob": ("bob", "user"),
                "akara.sup": ("12345", "user"),
                "surapol.gits": ("12345", "user"),
            }
            for u, (pwd, role) in demo.items():
                if u == "admin":
                    continue
                if not get_user(u):
                    create_user(u, pwd, role)
            app.logger.info("Seeded demo users (development only)")

    login_manager.init_app(app)

    # ---- Blueprints ----
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(payments_bp)

    # Exempt Payment BP from CSRF
    csrf.exempt(payments_bp)

    # Prometheus
    if _env_bool("METRICS_ENABLED", True):
        init_metrics(app)

    # ---- Errors ----

    @app.errorhandler(404)
    def not_found(e):
        app.logger.warning("404 %s %s", request.method, request.path)
        return render_template("errors/404.html", path=request.path), 404

    @app.errorhandler(405)
    def not_allowed(e):
        app.logger.warning("405 %s %s", request.method, request.path)
        return render_template("errors/404.html", path=request.path), 405

    # ---- Routes ----
    @app.get("/")
    def root():
        return redirect(url_for("playground"))

    @app.get("/playground")
    def playground():
        return render_template("playground.html")

    @app.before_request
    def _start_timer():
        g._t0 = time()

    @app.after_request
    def _log_request(resp):
        try:
            ms = (time() - getattr(g, "_t0", time())) * 1000
            app.logger.info("%s %s %s %s %.1fms",
                            request.remote_addr, request.method, request.full_path, resp.status_code, ms)

            # --- Skip self-scrapes & static to keep series clean ---
            ep = request.endpoint or ""
            path = request.path or ""
            if ep == "static" or path.startswith("/metrics"):
                return resp

            endpoint = ep.replace(".", "_") or "unknown"
            method = request.method
            status = str(resp.status_code)

            REQUEST_COUNT.labels(
                method=method, endpoint=endpoint, status=status).inc()
            REQUEST_LATENCY.labels(
                endpoint=endpoint, method=method).observe(ms / 1000.0)
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


if __name__ == "__main__":
    # TIP: use APP_ENV=production FLASK_SECRET_KEY=... when deploying
    app = create_app()
    app.run(host="0.0.0.0", port=8000, debug=(
        app.config["APP_ENV"] != "production"))
