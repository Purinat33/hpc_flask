# controllers/api.py
import hashlib
import json
from flask import Blueprint, request, jsonify, Response
from flask_login import login_required
from controllers.auth import admin_required
from models.rates_store import load_rates, save_rates
from models import rates_store

api_bp = Blueprint("api", __name__)


@api_bp.get("/formula")
def get_formula():
    tier = (request.args.get("type") or "mu").lower()
    rates = rates_store.load_rates()
    if tier not in rates:
        return jsonify({"error": f"unknown type '{tier}'"}), 400

    payload = {
        "type": tier,
        "unit": "per-hour",
        "rates": rates[tier],
        "currency": "THB",
    }

    # Strong/weak ETag both fine; use a hash of the payload
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    etag = 'W/"' + hashlib.sha256(body).hexdigest() + '"'

    # If client sends prior ETag and nothing changed → 304
    inm = request.headers.get("If-None-Match")
    if inm == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    resp = jsonify(payload)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@api_bp.post("/formula")
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

    rates = rates_store.load_rates()
    rates[tier] = {"cpu": cpu, "gpu": gpu, "mem": mem}
    save_rates(rates)
    return jsonify({"ok": True, "updated": {tier: rates[tier]}})
