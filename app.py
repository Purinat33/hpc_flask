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

def create_app():
    app = Flask(__name__, instance_relative_config=True)
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
    with app.app_context():
        init_db()
    login_manager.init_app(app)

    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(api_bp)

    # Routes that render templates
    @app.get("/")
    def root():
        return redirect(url_for("playground"))

    @app.get("/playground")
    def playground():
        return render_template("playground.html", NAV=render_nav("home"))

    return app


# Keep a module-level `app` so `flask --app app run` works
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
