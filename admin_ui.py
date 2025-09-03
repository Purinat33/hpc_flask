# admin_ui.py
from flask import Blueprint, request, render_template_string, redirect, url_for, flash
from flask_login import login_required, current_user
from auth import admin_required
from rates_store import load_rates, save_rates

admin_bp = Blueprint("admin", __name__)
PAGE = """
<!doctype html><title>Rates Admin</title>
<style>
  :root { --b:#1f7aec; --bg:#fff; --muted:#666; --bd:#e5e7eb; --hi:#eef4ff;}
  body{font-family:system-ui,Arial;margin:2rem;background:var(--bg)}
  .card{max-width:820px;padding:1rem 1.25rem;border:1px solid var(--bd);border-radius:12px;margin-bottom:1rem;background:#fff}
  label{display:block;margin-top:.5rem;font-weight:600}
  input,select{width:100%;padding:.6rem;border:1px solid #bbb;border-radius:8px}
  button{margin-top:1rem;padding:.6rem 1rem;border:0;border-radius:8px;background:var(--b);color:#fff;cursor:pointer}
  .muted{color:var(--muted);font-size:.92rem}
  table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--bd);border-radius:10px;overflow:hidden}
  th,td{padding:.65rem .8rem;border-bottom:1px solid var(--bd);text-align:left}
  thead th{background:#f8fafc;font-weight:700}
  tbody tr:last-child td{border-bottom:0}
  .tier{font-weight:700}
  tr.active{background:var(--hi)}
  .chip{display:inline-block;background:#f3f4f6;border-radius:999px;padding:.25rem .6rem;margin:.25rem .35rem 0 0;font-size:.85rem}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
</style>

<h2>Update Rates</h2>
<p class="muted">Signed in as <b>{{ current_user.username }}</b> (role: {{ current_user.role }}) — <a href="/logout">Logout</a></p>

<div class="card">
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for m in messages %}<div>{{ m }}</div>{% endfor %}
    {% endif %}
  {% endwith %}

  <form method="post">
    <label>Tier</label>
    <select name="type">
      <option value="mu" {% if tier=='mu' %}selected{% endif %}>mu</option>
      <option value="gov" {% if tier=='gov' %}selected{% endif %}>gov</option>
      <option value="private" {% if tier=='private' %}selected{% endif %}>private</option>
    </select>

    <div class="row">
      <div>
        <label>CPU (฿/cpu-hour)
          <input type="number" step="0.01" min="0" name="cpu" value="{{ '%.2f'|format(current['cpu']) }}">
        </label>
      </div>
      <div>
        <label>GPU (฿/gpu-hour)
          <input type="number" step="0.01" min="0" name="gpu" value="{{ '%.2f'|format(current['gpu']) }}">
        </label>
      </div>
    </div>

    <label>MEM (฿/GB-hour)
      <input type="number" step="0.01" min="0" name="mem" value="{{ '%.2f'|format(current['mem']) }}">
    </label>

    <div class="muted">
      <span class="chip">Selected: {{ tier|upper }}</span>
      <span class="chip">CPU: ฿{{ '%.2f'|format(current['cpu']) }}</span>
      <span class="chip">GPU: ฿{{ '%.2f'|format(current['gpu']) }}</span>
      <span class="chip">MEM: ฿{{ '%.2f'|format(current['mem']) }}</span>
    </div>

    <button type="submit">Update</button>
  </form>
</div>

<div class="card">
  <h3>Current Rates (All Tiers)</h3>
  <table>
    <thead>
      <tr>
        <th>Tier</th>
        <th>CPU (฿/cpu-hour)</th>
        <th>GPU (฿/gpu-hour)</th>
        <th>MEM (฿/GB-hour)</th>
      </tr>
    </thead>
    <tbody>
      {% for name in tiers %}
        {% set r = all_rates.get(name, {'cpu':0,'gpu':0,'mem':0}) %}
        <tr class="{{ 'active' if name==tier else '' }}">
          <td class="tier">{{ name|upper }}</td>
          <td>฿{{ '%.2f'|format(r['cpu']) }}</td>
          <td>฿{{ '%.2f'|format(r['gpu']) }}</td>
          <td>฿{{ '%.2f'|format(r['mem']) }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="muted" style="margin-top:.5rem">
    Formula: <code>cost = (CPU × hours × cpu_rate) + (GPU × hours × gpu_rate) + (MemGB × hours × mem_rate)</code>
  </p>
</div>
"""


@admin_bp.get("/admin")
@login_required
@admin_required
def admin_form():
    rates = load_rates()
    tier = (request.args.get("type") or "mu").lower()
    if tier not in rates:
        tier = "mu"
    return render_template_string(
        PAGE,
        all_rates=rates,
        current=rates[tier],
        tier=tier,
        tiers=['mu', 'gov', 'private'],
        current_user=current_user
    )


@admin_bp.post("/admin")
@login_required
@admin_required
def admin_update():
    tier = (request.form.get("type") or "").lower()
    try:
        cpu = float(request.form.get("cpu", "0"))
        gpu = float(request.form.get("gpu", "0"))
        mem = float(request.form.get("mem", "0"))
    except Exception:
        flash("Invalid numeric input")
        return redirect(url_for("admin.admin_form", type=tier or "mu"))

    if tier not in {"mu", "gov", "private"}:
        flash("Type must be one of mu|gov|private")
        return redirect(url_for("admin.admin_form"))

    if min(cpu, gpu, mem) < 0:
        flash("Rates must be ≥ 0")
        return redirect(url_for("admin.admin_form", type=tier))

    rates = load_rates()
    rates[tier] = {"cpu": cpu, "gpu": gpu, "mem": mem}
    save_rates(rates)
    flash(f"Updated {tier} → {rates[tier]}")
    return redirect(url_for("admin.admin_form", type=tier))
