# user_ui.py
from billing_store import list_receipts, get_receipt_with_items
from flask import Blueprint, request, render_template_string, Response, url_for
from flask_login import login_required, current_user
from datetime import date, timedelta
import io
from ui_base import nav as render_nav
from data_sources import fetch_jobs_with_fallbacks
# your existing function that adds CPU_Core_Hours, GPU_Hours, Mem_GB_Hours, tier, Cost (฿)
from billing import compute_costs
from billing_store import billed_job_ids, canonical_job_id
from flask import flash, redirect
from billing_store import create_receipt_from_rows
# add to imports at top
from billing_store import list_billed_items_for_user


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
  .tabs{display:inline-flex;border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin:.5rem 0}
  .tabs a{padding:.4rem .7rem;text-decoration:none;color:#1f2937;border-right:1px solid var(--bd)}
  .tabs a:last-child{border-right:0}
  .tabs a.on{background:#eef4ff;color:#1f7aec;font-weight:700}
</style>

{{ NAV|safe }}
<h2>My Usage</h2>
<p class="muted">Signed in as <b>{{ current_user.username }}</b> — <a href="/logout">Logout</a></p>

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

  <form method="post" action="{{ url_for('user.create_receipt') }}" style="display:inline">
    <input type="hidden" name="start" value="{{ start }}">
    <input type="hidden" name="end" value="{{ end }}">
    <button type="submit">Create Receipt</button>
  </form>

  <div class="tabs">
    <a class="{{ 'on' if view=='detail' else '' }}"
       href="{{ url_for('user.my_usage', start=start, end=end, view='detail') }}">Detailed</a>
    <a class="{{ 'on' if view=='aggregate' else '' }}"
       href="{{ url_for('user.my_usage', start=start, end=end, view='aggregate') }}">Aggregate</a>
    <a class="{{ 'on' if view=='billed' else '' }}"
       href="{{ url_for('user.my_usage', start=start, end=end, view='billed') }}">Billed</a>
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

  {% elif view == 'aggregate' %}
    {% if agg_rows and agg_rows|length>0 %}
      <table>
        <thead>
          <tr>
            <th>Jobs</th><th>CPU core-hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Total Cost (฿)</th>
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

  {% elif view == 'billed' %}
    <h4>Pending (unpaid) jobs</h4>
    {% if pending and pending|length>0 %}
      <p class="muted">
        <span class="chip">Jobs: {{ pending|length }}</span>
        <span class="chip">Total: ฿{{ '%.2f'|format(sum_pending) }}</span>
      </p>
      <table>
        <thead>
          <tr>
            <th>Receipt</th><th>Period</th><th>Job</th>
            <th>CPU hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Cost (฿)</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {% for it in pending %}
            <tr>
              <td><a href="{{ url_for('user.view_receipt', rid=it['receipt_id']) }}">#{{ it['receipt_id'] }}</a></td>
              <td>{{ it['start'] }} → {{ it['end'] }}</td>
              <td>{{ it['job_id_display'] }}</td>
              <td>{{ '%.2f'|format(it['cpu_core_hours']) }}</td>
              <td>{{ '%.2f'|format(it['gpu_hours']) }}</td>
              <td>{{ '%.2f'|format(it['mem_gb_hours']) }}</td>
              <td>฿{{ '%.2f'|format(it['cost']) }}</td>
              <td>{{ it['created_at'] }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No pending jobs.</p>
    {% endif %}

    <h4 style="margin-top:1rem">Paid jobs</h4>
    {% if paid and paid|length>0 %}
      <p class="muted">
        <span class="chip">Jobs: {{ paid|length }}</span>
        <span class="chip">Total: ฿{{ '%.2f'|format(sum_paid) }}</span>
      </p>
      <table>
        <thead>
          <tr>
            <th>Receipt</th><th>Period</th><th>Job</th>
            <th>CPU hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Cost (฿)</th><th>Paid at</th>
          </tr>
        </thead>
        <tbody>
          {% for it in paid %}
            <tr>
              <td><a href="{{ url_for('user.view_receipt', rid=it['receipt_id']) }}">#{{ it['receipt_id'] }}</a></td>
              <td>{{ it['start'] }} → {{ it['end'] }}</td>
              <td>{{ it['job_id_display'] }}</td>
              <td>{{ '%.2f'|format(it['cpu_core_hours']) }}</td>
              <td>{{ '%.2f'|format(it['gpu_hours']) }}</td>
              <td>{{ '%.2f'|format(it['mem_gb_hours']) }}</td>
              <td>฿{{ '%.2f'|format(it['cost']) }}</td>
              <td>{{ it['paid_at'] or '—' }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No paid jobs yet.</p>
    {% endif %}

  {% else %}
    <p class="muted">Unknown view.</p>
  {% endif %}
</div>
"""


RECEIPTS_PAGE = """
<!doctype html><title>My Receipts</title>
<style>
body{font-family:system-ui,Arial;margin:2rem}
.card{max-width:1100px;padding:1rem 1.25rem;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:1rem;background:#fff}
table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden}
th,td{padding:.55rem .7rem;border-bottom:1px solid #e5e7eb;text-align:left;font-size:.94rem}
thead th{background:#f8fafc;font-weight:700}
.chip{display:inline-block;background:#f3f4f6;border-radius:999px;padding:.25rem .6rem;margin:.25rem .35rem 0 0;font-size:.85rem}
</style>
{{ NAV|safe }}
<h2>My Receipts</h2>
<div class="card">
  {% if receipts %}
  <table>
    <thead><tr><th>ID</th><th>Period</th><th>Status</th><th>Total (฿)</th><th>Created</th></tr></thead>
    <tbody>
      {% for r in receipts %}
        <tr>
          <td><a href="{{ url_for('user.view_receipt', rid=r['id']) }}">#{{ r['id'] }}</a></td>
          <td>{{ r['start'] }} → {{ r['end'] }}</td>
          <td>{{ r['status'] }}</td>
          <td>฿{{ '%.2f'|format(r['total']) }}</td>
          <td>{{ r['created_at'] }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="muted">No receipts yet.</p>
  {% endif %}
</div>
"""


@user_bp.get("/me/receipts")
@login_required
def my_receipts():
    return render_template_string(
        RECEIPTS_PAGE,
        NAV=render_nav("usage"),
        receipts=list_receipts(current_user.username)
    )


@user_bp.get("/me/receipts/<int:rid>")
@login_required
def view_receipt(rid: int):
    rec, items = get_receipt_with_items(rid)
    if not rec or rec.get("username") != current_user.username:
        return redirect(url_for("user.my_receipts"))
    # very simple detail view
    rows = items
    return render_template_string("""
    <!doctype html><title>Receipt #{{ r['id'] }}</title>
    <style>body{font-family:system-ui,Arial;margin:2rem}table{width:100%;border-collapse:collapse}th,td{border:1px solid #ddd;padding:.5rem}</style>
    {{ NAV|safe }}
    <h2>Receipt #{{ r['id'] }} — {{ r['status'] }}</h2>
    <p>Period: {{ r['start'] }} → {{ r['end'] }}</p>
    <p>Total: ฿{{ '%.2f'|format(r['total']) }}</p>
    <table>
      <thead><tr><th>Job</th><th>CPU hrs</th><th>GPU hrs</th><th>Mem GB-hrs</th><th>Cost (฿)</th></tr></thead>
      <tbody>
        {% for it in rows %}
        <tr>
          <td>{{ it['job_id_display'] }}</td>
          <td>{{ '%.2f'|format(it['cpu_core_hours']) }}</td>
          <td>{{ '%.2f'|format(it['gpu_hours']) }}</td>
          <td>{{ '%.2f'|format(it['mem_gb_hours']) }}</td>
          <td>{{ '%.2f'|format(it['cost']) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    """, NAV=render_nav("usage"), r=rec, rows=rows)


@user_bp.get("/me")
@login_required
def my_usage():
    end_d = request.args.get("end") or date.today().isoformat()
    start_d = request.args.get("start") or (
        date.today() - timedelta(days=7)).isoformat()
    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate", "billed"}:
        view = "detail"

    rows, agg_rows = [], []
    data_source = None
    notes = []
    total_cost = 0.0
    pending = paid = []
    sum_pending = sum_paid = 0.0

    try:
        if view in {"detail", "aggregate"}:
            df, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)
            df = compute_costs(df)

            # Hide jobs that are already billed/pending
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            already = billed_job_ids()
            df = df[~df["JobKey"].isin(already)]

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

        else:  # view == 'billed'
            pending = list_billed_items_for_user(
                current_user.username, "pending")
            paid = list_billed_items_for_user(current_user.username, "paid")
            sum_pending = float(sum(i["cost"]
                                for i in pending)) if pending else 0.0
            sum_paid = float(sum(i["cost"] for i in paid)) if paid else 0.0

    except Exception as e:
        notes.append(str(e))

    return render_template_string(
        PAGE,
        NAV=render_nav("usage"),
        current_user=current_user,
        start=start_d, end=end_d, view=view,
        rows=rows,                 # detailed
        agg_rows=agg_rows,         # aggregate
        pending=pending, paid=paid, sum_pending=sum_pending, sum_paid=sum_paid,  # billed
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


@user_bp.post("/me/receipt")
@login_required
def create_receipt():
    end_d = request.form.get("end") or date.today().isoformat()
    start_d = request.form.get("start") or (
        date.today() - timedelta(days=7)).isoformat()

    # Re-fetch, recompute, hide billed (server-side safety)
    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)
    df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
    df = df[~df["JobKey"].isin(billed_job_ids())]

    if df.empty:
        flash("No unbilled jobs to create a receipt from.")
        return redirect(url_for("user.my_usage", start=start_d, end=end_d))

    rid, total, skipped = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records"))
    msg = f"Created receipt #{rid} for ฿{total:.2f}"
    if skipped:
        msg += f" (skipped {len(skipped)} already billed job(s))"
    flash(msg)
    return redirect(url_for("user.my_receipts"))
