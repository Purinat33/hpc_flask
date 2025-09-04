# app.py
import os
from flask import Flask, render_template, request, jsonify, render_template_string, abort, redirect, url_for
from flask_login import login_required, current_user
from rates_store import load_rates, save_rates
from admin_ui import admin_bp
from auth import auth_bp, login_manager, admin_required
from user_ui import user_bp
from ui_base import nav as render_nav
from db import init_app as init_db_app, init_db


app = Flask(__name__)

init_db_app(app)
with app.app_context():
    init_db()


app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")


# Init Flask-Login
login_manager.init_app(app)

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(user_bp)
# --- API (GET remains public, POST now requires admin session) ---


@app.get("/formula")
def get_formula():
    tier = (request.args.get("type") or "mu").lower()
    rates = load_rates()
    if tier not in rates:
        return jsonify({"error": f"unknown type '{tier}'"}), 400
    return jsonify({"type": tier, "unit": "per-hour",
                    "rates": rates[tier], "currency": "THB"})


@app.post("/formula")
@login_required
@admin_required
def update_formula():
    payload = request.get_json(force=True, silent=True) or {}
    tier = (payload.get("type") or "").lower()
    if tier not in {"mu", "gov", "private"}:
        return jsonify({"error": "type must be one of mu|gov|private"}), 400
    try:
        cpu = float(payload["cpu"])
        gpu = float(payload["gpu"])
        mem = float(payload["mem"])
    except Exception:
        return jsonify({"error": "cpu, gpu, mem must be numeric"}), 400

    rates = load_rates()
    rates[tier] = {"cpu": cpu, "gpu": gpu, "mem": mem}
    save_rates(rates)
    return jsonify({"ok": True, "updated": {tier: rates[tier]}})


@app.get("/")
def root():
    return redirect(url_for("playground"))


@app.get("/playground")
def playground():
    return render_template('playground.html',  NAV=render_nav("home"))


if __name__ == "__main__":
    # Example: set ADMIN_PASSWORD for the dummy admin user
    # set FLASK_SECRET_KEY, run with a real reverse proxy/HTTPS in prod
    print("Started app.py")
    app.run(host="0.0.0.0", port=8000, debug=True)
