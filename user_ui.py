# user_ui.py
from flask import Blueprint, request, render_template_string, Response, url_for
from flask_login import login_required, current_user
from datetime import date, timedelta
import io
from ui_base import nav as render_nav
from data_sources import fetch_jobs_with_fallbacks
# your existing function that adds CPU_Core_Hours, GPU_Hours, Mem_GB_Hours, tier, Cost (฿)
from billing import compute_costs

user_bp = Blueprint("user", __name__)

PAGE = """
<!doctype html><title>My Usage</title>
<style>
  :root { --b:#1f7aec; --bg:#fff; --muted:#666; --bd:#e5e7eb; --hi:#eef4ff;}
  body{font-family:system-ui,Arial;margin:2rem;background:var(--bg)}
  .card{max-width:1100px;padding:1rem 1.25rem;border:1px solid var(--bd);border-radius:12px;margin-bottom:1rem;background:#fff}
  label{display:block;margin-top:.5rem;font-weight:600}
  input{width:100%;padding:.6rem;border:1px solid #bbb;border-radius:8px}
  button{margin-top:1rem;padding:.6rem 1rem;border:0;border-radius:8px;background:var(--b);color:#fff;cursor:pointer}
  .muted{color:var(--muted);font-size:.92rem}
  table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--bd);border-radius:10px;overflow:hidden}
  th,td{padding:.55rem .7rem;border-bottom:1px solid var(--bd);text-align:left;font-size:.94rem}
  thead th{background:#f8fafc;font-weight:700}
  tbody tr:last-child td{border-bottom:0}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem}
  .chip{display:inline-block;background:#f3f4f6;border-radius:999px;padding:.25rem .6rem;margin:.25rem .35rem 0 0;font-size:.85rem}
</style>
{{ NAV|safe }}
<h2>My Usage</h2>
<p class="muted">Signed in as <b>{{ current_user.username }}</b> — <a href="/logout">Logout</a></p>

{# user_ui.py PAGE string: insert tabs + either detailed or aggregate table #}

<div class="card">
  <h3>Filter</h3>
  <form method="get" class="grid">
    <div><label>Start date<input type="date" name="start" value="{{ start }}"></label></div>
    <div><label>End date<input type="date" name="end" value="{{ end }}"></label></div>
    <div><label>&nbsp;<button type="submit">Fetch</button></label></div>
  </form>
  {% if data_source %}
    <p class="muted">Source: <b>{{ data_source }}</b>{% if notes and notes|length>0 %} — {{ notes|join(' | ') }}{% endif %}</p>
  {% endif %}
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <h3>Your Jobs</h3>
    <div>
      <a href="{{ url_for('user.my_usage_csv', start=start, end=end) }}"><button type="button">Download CSV</button></a>
    </div>
  </div>

  <style>
    .tabs{display:inline-flex;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;margin:.5rem 0}
    .tabs a{padding:.4rem .7rem;text-decoration:none;color:#1f2937;border-right:1px solid #e5e7eb}
    .tabs a:last-child{border-right:0}
    .tabs a.on{background:#eef4ff;color:#1f7aec;font-weight:700}
  </style>
  <div class="tabs">
    <a class="{{ 'on' if view=='detail' else '' }}"
       href="{{ url_for('user.my_usage', start=start, end=end, view='detail') }}">Detailed</a>
    <a class="{{ 'on' if view=='aggregate' else '' }}"
       href="{{ url_for('user.my_usage', start=start, end=end, view='aggregate') }}">Aggregate</a>
  </div>

  {% if view == 'detail' %}
    {% if rows and rows|length>0 %}
      <p class="muted">
        <span class="chip">Jobs: {{ rows|length }}</span>
        <span class="chip">Total cost: ฿{{ '%.2f'|format(total_cost) }}</span>
      </p>
      <table>
        <thead>
          <tr>
            <th>JobID</th><th>Elapsed</th><th>TotalCPU</th><th>ReqTRES</th>
            <th>CPU core-hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Tier</th><th>Cost (฿)</th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
            <tr>
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
    {% else %}
      <p class="muted">No jobs for the selected period.</p>
    {% endif %}
  {% else %}
    {% if agg_rows and agg_rows|length>0 %}
      <table>
        <thead>
          <tr>
            <th>Jobs</th>
            <th>CPU core-hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Total Cost (฿)</th>
          </tr>
        </thead>
        <tbody>
          {% for r in agg_rows %}
            <tr>
              <td>{{ r['jobs'] }}</td>
              <td>{{ '%.2f'|format(r['CPU_Core_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['GPU_Hours']) }}</td>
              <td>{{ '%.2f'|format(r['Mem_GB_Hours']) }}</td>
              <td>฿{{ '%.2f'|format(r['Cost (฿)']) }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No jobs for the selected period.</p>
    {% endif %}
  {% endif %}
</div>

"""


@user_bp.get("/me")
@login_required
def my_usage():
    end_d = request.args.get("end") or date.today().isoformat()
    start_d = request.args.get("start") or (
        date.today() - timedelta(days=7)).isoformat()
    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate"}:
        view = "detail"

    rows, agg_rows = [], []
    data_source = None
    notes = []
    total_cost = 0.0

    try:
        df, data_source, notes = fetch_jobs_with_fallbacks(
            start_d, end_d, username=current_user.username)
        df = compute_costs(df)

        # detailed rows
        cols = ["JobID", "Elapsed", "TotalCPU", "ReqTRES",
                "CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours", "tier", "Cost (฿)"]
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        rows = df[cols].to_dict(orient="records")
        total_cost = float(df["Cost (฿)"].sum()) if not df.empty else 0.0

        # aggregate (single row)
        if not df.empty:
            agg_row = {
                "user": current_user.username,
                "tier": (df["tier"].mode()[0] if not df["tier"].mode().empty else ""),
                "jobs": int(len(df)),
                "CPU_Core_Hours": float(df["CPU_Core_Hours"].sum()),
                "GPU_Hours": float(df["GPU_Hours"].sum()),
                "Mem_GB_Hours": float(df["Mem_GB_Hours"].sum()),
                "Cost (฿)": float(df["Cost (฿)"].sum()),
            }
            agg_rows = [agg_row]
    except Exception as e:
        notes.append(str(e))

    return render_template_string(
        PAGE,
        NAV=render_nav("usage"),
        current_user=current_user,
        start=start_d, end=end_d, view=view,
        rows=rows,             # detailed
        agg_rows=agg_rows,     # aggregated (single row)
        data_source=data_source, notes=notes,
        total_cost=total_cost,
        url_for=url_for
    )


@user_bp.get("/me.csv")
@login_required
def my_usage_csv():
    end_d = request.args.get("end") or date.today().isoformat()
    start_d = request.args.get("start") or (
        date.today() - timedelta(days=7)).isoformat()

    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)

    out = io.StringIO()
    df.to_csv(out, index=False)
    out.seek(0)
    filename = f"usage_{current_user.username}_{start_d}_{end_d}.csv"
    return Response(out.read(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})
