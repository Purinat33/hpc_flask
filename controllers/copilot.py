# controllers/copilot.py
from flask import Blueprint, request, jsonify, current_app, send_from_directory
import os
from services.copilot import ask, rebuild

copilot_bp = Blueprint("copilot", __name__)


@copilot_bp.post("/copilot/ask")
def copilot_ask():
    if not current_app.config.get("COPILOT_ENABLED", True):
        return jsonify({"answer_html": "Copilot disabled.", "sources": []}), 503
    q = (request.json or {}).get("q", "").strip()
    if not q:
        return jsonify({"answer_html": "Ask me something about this app.", "sources": []})
    ip = request.headers.get(
        "X-Forwarded-For", request.remote_addr or "0.0.0.0").split(",")[0].strip()
    try:
        return jsonify(ask(ip, q))
    except Exception as e:
        current_app.logger.exception("copilot ask failed")
        return jsonify({"answer_html": f"Copilot error: {e}", "sources": []}), 500


@copilot_bp.post("/copilot/reindex")
def copilot_reindex():
    # optionally restrict behind admin auth if you like
    rebuild()
    return jsonify({"ok": True})


@copilot_bp.get("/copilot/widget.js")
def copilot_widget_js():
    # if you place the JS in /static/js, you can just serve it from there instead
    return send_from_directory(os.path.join(current_app.root_path, "static", "js"), "copilot-widget.js", mimetype="text/javascript")
