# admin_ui.py
from flask import Blueprint, request, render_template_string, redirect, url_for, flash
from flask_login import login_required, current_user
from auth import admin_required
from rates_store import load_rates, save_rates
from data_sources import fetch_jobs_with_fallbacks
from billing import compute_costs
from datetime import date, timedelta
from ui_base import nav as render_nav

admin_bp = Blueprint("admin", __name__)

PAGE = """
<!doctype html><title>Rates Admin</title>
<style>
  :root { --b:#1f7aec; --bg:#fff; --muted:#666; --bd:#e5e7eb; --hi:#eef4ff;}
  body{font-family:system-ui,Arial;margin:2rem;background:var(--bg)}
  .card{max-width:1100px;padding:1rem 1.25rem;border:1px solid var(--bd);border-radius:12px;margin-bottom:1rem;background:#fff}
  label{display:block;margin-top:.5rem;font-weight:600}
  input,select{width:100%;padding:.6rem;border:1px solid #bbb;border-radius:8px}
  button{margin-top:1rem;padding:.6rem 1rem;border:0;border-radius:8px;background:var(--b);color:#fff;cursor:pointer}
  .muted{color:var(--muted);font-size:.92rem}
  table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--bd);border-radius:10px;overflow:hidden}
  th,td{padding:.55rem .7rem;border-bottom:1px solid var(--bd);text-align:left;font-size:.94rem}
  thead th{background:#f8fafc;font-weight:700}
  tbody tr:last-child td{border-bottom:0}
  .tier{font-weight:700}
  tr.active{background:var(--hi)}
  .chip{display:inline-block;background:#f3f4f6;border-radius:999px;padding:.25rem .6rem;margin:.25rem .35rem 0 0;font-size:.85rem}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
  .grid2{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem}
</style>
{{ NAV|safe }}
<h2>Update Rates</h2>
<p class="muted">Signed in as <b>{{ current_user.username }}</b> (role: {{ current_user.role }}) — <a href="/logout">Logout</a></p>

<div class="card">
  {% with messages = get_flashed_messages() %}
    {% if messages %}{% for m in messages %}<div>{{ m }}</div>{% endfor %}{% endif %}
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
      <tr><th>Tier</th><th>CPU (฿/cpu-hour)</th><th>GPU (฿/gpu-hour)</th><th>MEM (฿/GB-hour)</th></tr>
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
    Formula: <code>cost = (CPU_core_hours × cpu_rate) + (GPU_hours × gpu_rate) + (MemGB_hours × mem_rate)</code>
  </p>
</div>

{# admin_ui.py PAGE string: insert tabs + two tables #}

<div class="card">
  <h3>Usage Preview (slurmrestd → sacct → test.csv)</h3>
  <form method="get">
    <div class="grid2">
      <div><label>Start date<input type="date" name="start" value="{{ start }}"></label></div>
      <div><label>End date<input type="date" name="end" value="{{ end }}"></label></div>
      <div><label>&nbsp;<button type="submit">Fetch Usage</button></label></div>
    </div>
    <input type="hidden" name="type" value="{{ tier }}">
  </form>

  <style>
    .tabs{display:inline-flex;border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin:.75rem 0}
    .tabs a{padding:.4rem .7rem;text-decoration:none;color:#1f2937;border-right:1px solid var(--bd)}
    .tabs a:last-child{border-right:0}
    .tabs a.on{background:#eef4ff;color:#1f7aec;font-weight:700}
    .right{float:right}
    .clear{clear:both}
  </style>
  <div class="tabs">
    <a class="{{ 'on' if view=='detail' else '' }}"
       href="{{ url_for('admin.admin_form', start=start, end=end, type=tier, view='detail') }}">Detailed</a>
    <a class="{{ 'on' if view=='aggregate' else '' }}"
       href="{{ url_for('admin.admin_form', start=start, end=end, type=tier, view='aggregate') }}">Aggregate</a>
  </div>
  <div class="right muted">Source: <b>{{ data_source or '—' }}</b>{% if notes and notes|length>0 %} — {{ notes|join(' | ') }}{% endif %}</div>
  <div class="clear"></div>

  {% if view == 'detail' %}
    {% if rows and rows|length>0 %}
      <table>
        <thead>
          <tr>
            <th>User</th><th>JobID</th><th>Elapsed</th><th>TotalCPU</th><th>ReqTRES</th>
            <th>CPU core-hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Tier</th><th>Cost (฿)</th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['User'] }}</td>
              <td>{{ r['JobID'] }}</td>
              <td>{{ r['Elapsed'] }}</td>
              <td>{{ r['TotalCPU'] }}</td>
              <td>{{ r['ReqTRES'] }}</td>
              <td>{{ '%.2f'|format(r['CPU_Core_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['GPU_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['Mem_GB_Hours']) }}</td>
              <td>{{ r['tier']|upper }}</td>
              <td>฿{{ '%.2f'|format(r['Cost (฿)']) }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
            <div class="muted" style="margin-top:.5rem">
        <span class="chip">CPU core-hrs: {{ '%.2f'|format(tot_cpu) }}</span>
        <span class="chip">GPU hrs: {{ '%.2f'|format(tot_gpu) }}</span>
        <span class="chip">Mem GB-hrs: {{ '%.2f'|format(tot_mem) }}</span>
        <span class="chip">Elapsed hrs: {{ '%.2f'|format(tot_elapsed) }}</span>
      </div>

    {% else %}
      <p class="muted">No rows for the given range.</p>
    {% endif %}
  {% else %}
    {% if agg_rows and agg_rows|length>0 %}
      <p class="muted"><span class="chip">Grand total: ฿{{ '%.2f'|format(grand_total) }}</span></p>
      <table>
        <thead>
          <tr>
            <th>User</th><th>Tier</th><th>Jobs</th>
            <th>CPU core-hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Total Cost (฿)</th>
          </tr>
        </thead>
        <tbody>
          {% for r in agg_rows %}
            <tr>
              <td>{{ r['User'] }}</td>
              <td>{{ r['tier']|upper }}</td>
              <td>{{ r['jobs'] }}</td>
              <td>{{ '%.2f'|format(r['CPU_Core_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['GPU_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['Mem_GB_Hours']) }}</td>
              <td>฿{{ '%.2f'|format(r['Cost (฿)']) }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
            <div class="muted" style="margin-top:.5rem">
        <span class="chip">CPU core-hrs: {{ '%.2f'|format(tot_cpu) }}</span>
        <span class="chip">GPU hrs: {{ '%.2f'|format(tot_gpu) }}</span>
        <span class="chip">Mem GB-hrs: {{ '%.2f'|format(tot_mem) }}</span>
        <span class="chip">Elapsed hrs: {{ '%.2f'|format(tot_elapsed) }}</span>
      </div>

    {% else %}
      <p class="muted">No rows for the given range.</p>
    {% endif %}
  {% endif %}
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

    # read toggle
    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate"}:
        view = "detail"

    end_d = request.args.get("end") or date.today().isoformat()
    start_d = request.args.get("start") or (
        date.today() - timedelta(days=7)).isoformat()

    rows, agg_rows = [], []
    grand_total = 0.0
    data_source = None
    notes = []
    try:
        df, data_source, notes = fetch_jobs_with_fallbacks(start_d, end_d)
        df = compute_costs(df)
        # --- totals for chips below the table ---
        tot_cpu = tot_gpu = tot_mem = tot_elapsed = 0.0
        if not df.empty:
            tot_cpu = float(df["CPU_Core_Hours"].sum())
            tot_gpu = float(df["GPU_Hours"].sum())
            tot_mem = float(df["Mem_GB_Hours"].sum())
            # compute_costs already creates Elapsed_Hours
            tot_elapsed = float(df.get("Elapsed_Hours", 0).sum())

        # Detailed rows (existing)
        cols = ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES",
                "CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours", "tier", "Cost (฿)"]
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        rows = df[cols].to_dict(orient="records")

        # Aggregate rows (new): one per user
        if not df.empty:
            agg = (
                df.groupby(["User", "tier"], dropna=False)
                .agg(
                    jobs=("JobID", "count"),
                    CPU_Core_Hours=("CPU_Core_Hours", "sum"),
                    GPU_Hours=("GPU_Hours", "sum"),
                    Mem_GB_Hours=("Mem_GB_Hours", "sum"),
                    Cost=("Cost (฿)", "sum"),
                )
                .reset_index()
            )
            agg.rename(columns={"Cost": "Cost (฿)"}, inplace=True)
            agg_rows = agg[
                ["User", "tier", "jobs", "CPU_Core_Hours",
                    "GPU_Hours", "Mem_GB_Hours", "Cost (฿)"]
            ].to_dict(orient="records")
            grand_total = float(agg["Cost (฿)"].sum())
    except Exception as e:
        notes.append(str(e))

    return render_template_string(
        PAGE,
        NAV=render_nav("usage"),
        all_rates=rates, current=rates[tier], tier=tier, tiers=[
            "mu", "gov", "private"],
        current_user=current_user,
        start=start_d, end=end_d,
        view=view,
        rows=rows,               # detailed
        agg_rows=agg_rows,       # aggregated
        grand_total=grand_total,
        data_source=data_source, notes=notes,
        tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
        url_for=url_for
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
    # stay on same tier and same date range
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    q = {"type": tier}
    if start:
        q["start"] = start
    if end:
        q["end"] = end
    return redirect(url_for("admin.admin_form", **q))
