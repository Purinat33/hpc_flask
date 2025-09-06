# NVIDIA Bright Computing HPC Accounting Application

## Slurm Integration (Guide):

Here’s a concise, hand-off–ready manual for wiring **real Slurm** into your app for both **auth** (recommended: PAM/SSO against the cluster’s user directory) and **data fetching** (recommended: `slurmrestd`, with `sacct` as fallback). I’ve split it into requirements, setup on the cluster, app-side file changes (exact files/lines), and a short runbook. Citations to Slurm docs are included.

---

### 1) What you’ll implement

- **Authentication (login)**

  - Prefer **PAM/SSO** against the cluster’s existing identity (LDAP/AD/Unix) so users log in with the same account they use for Slurm commands. (This is separate from Slurm’s internal daemon auth via Munge.)
  - Optionally: accept **Slurm JWT** (issued by `scontrol token`) to call `slurmrestd`. Tokens go in `X-SLURM-USER-TOKEN` (or cookie) to identify a user to the REST daemon. ([Slurm][1])

- **Usage data**

  - Primary: **`slurmrestd`** `/slurm/vX.Y.Z/jobs` (+ optional `/slurmdb` endpoints if you have SlurmDBD) with time filters → convert JSON → `pandas.DataFrame`. ([Slurm][1])
  - Fallback: **`sacct`** (`--parsable2`, `--format=User,JobID,Elapsed,TotalCPU,ReqTRES,End,State`, `-S/-E`, `--allusers` for admin view). ([Slurm][1])

---

### 2) Cluster-side requirements & setup

#### A. Slurm accounting & REST

1. **Accounting enabled**

   - `slurmdbd` running, cluster registered, accounting on. (You’ll use `sacct` and optionally `/slurmdb` REST.) ([Slurm][1])

2. **Start `slurmrestd`**

   - Use the packaged unit (`slurmrestd.service`) or run it under a reverse proxy with TLS.
   - Confirm versioned OpenAPI endpoints are enabled (default). The man page shows example curl with `X-SLURM-USER-TOKEN`. ([Slurm][1])

3. **JWT auth for REST (recommended)**

   - In `slurm.conf`, load JWT auth plugin (`AuthAltTypes=auth/jwt`) and configure JWT secrets; tokens are created with `scontrol token`. ([Debian Manpages][2])
   - Clients send the token via header or cookie (`X-SLURM-USER-TOKEN`). ([Slurm][1])

4. **Authorization scope for admin usage**

   - If your “admin” user must see **all users’ jobs** via `sacct`, set their **AdminLevel** in accounting (prefer **Operator** for read-only). In `slurmdbd.conf`/`sacctmgr`, `AdminLevel=Operator` provides limited admin privileges. (SlurmDBD `PrivateData` also affects visibility.) ([Debian Manpages][2])

> Notes: The `slurmrestd` docs show available OpenAPI plugins (including `slurmdb` endpoints) and the header/cookie names for JWT.

---

### 3) App-side changes (exact files)

#### A. Add a **standalone REST client** (new file): `services/slurmrest_client.py`

Create this file; it hides all REST specifics and outputs a **normalized DataFrame** your billing pipeline already understands.

```python
# services/slurmrest_client.py
import os
import requests
import pandas as pd
from datetime import datetime

def _sec_to_hms(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def query_jobs(start_date: str, end_date: str, username: str | None = None) -> pd.DataFrame:
    """
    Calls slurmrestd /slurm/v*/jobs and returns a DataFrame with columns:
      User, JobID, Elapsed, TotalCPU, ReqTRES, End, State
    Env:
      SLURMRESTD_URL          e.g. https://slurm-ctl.example:6820
      SLURMRESTD_TOKEN        (optional) JWT for X-SLURM-USER-TOKEN
      SLURMRESTD_VERSION      (optional) e.g. v0.0.39 (default)
      SLURMRESTD_VERIFY_TLS   "0" to skip TLS verify (dev only)
    """
    base = os.environ.get("SLURMRESTD_URL")
    if not base:
        raise RuntimeError("SLURMRESTD_URL not set")
    ver = os.environ.get("SLURMRESTD_VERSION", "v0.0.39")
    verify = os.environ.get("SLURMRESTD_VERIFY_TLS", "1") != "0"

    headers = {}
    tok = os.environ.get("SLURMRESTD_TOKEN")
    if tok:
        # Header is accepted per slurmrestd manpage (also supports cookie form)
        headers["X-SLURM-USER-TOKEN"] = tok

    url = f"{base.rstrip('/')}/slurm/{ver}/jobs"
    params = {
        "start_time": f"{start_date}T00:00:00",
        "end_time":   f"{end_date}T23:59:59",
    }
    # If your slurmrestd supports server-side user filter, add here (else filter client-side)
    if username:
        params["user_name"] = username

    r = requests.get(url, headers=headers, params=params, timeout=20, verify=verify)
    r.raise_for_status()
    js = r.json()

    rows = []
    for j in js.get("jobs", []):
        user = j.get("user_name") or j.get("user")
        jobid = j.get("job_id") or j.get("jobid")
        elapsed_s = j.get("elapsed") or (j.get("time") or {}).get("elapsed")
        totalcpu_s = (j.get("stats") or {}).get("total_cpu")
        tres = j.get("tres_req_str") or j.get("tres_fmt") or j.get("tres_req") or ""
        state = j.get("job_state") or j.get("state")
        end_ts = j.get("end_time") or (j.get("time") or {}).get("end")
        # normalize
        rows.append({
            "User": user or "",
            "JobID": jobid,
            "Elapsed": elapsed_s if isinstance(elapsed_s, str) else _sec_to_hms(elapsed_s),
            "TotalCPU": totalcpu_s if isinstance(totalcpu_s, str) else _sec_to_hms(totalcpu_s),
            "ReqTRES": tres,
            "End": datetime.utcfromtimestamp(end_ts).isoformat() if isinstance(end_ts, (int, float)) else (end_ts or ""),
            "State": state or "",
        })

    if not rows:
        # keep behavior consistent with your fallbacks
        raise RuntimeError("slurmrestd returned no jobs in the range")
    df = pd.DataFrame(rows)
    return df
```

> Why header? `slurmrestd` accepts JWT via cookie or header; the man page shows header usage (`X-SLURM-USER-TOKEN`). ([Slurm][1])

---

#### B. One-line swap inside `services/data_sources.py`

Replace the placeholder `fetch_from_slurmrestd` with a thin wrapper around the new client:

```python
# services/data_sources.py (replace the stubbed function)
from services.slurmrest_client import query_jobs  # NEW

def fetch_from_slurmrestd(start_date: str, end_date: str, username: str | None = None):
    df = query_jobs(start_date, end_date, username=username)
    # defensive End cutoff (same policy you already use)
    if "End" in df.columns:
        import pandas as pd
        df["End"] = pd.to_datetime(df["End"], errors="coerce")
        cutoff = pd.to_datetime(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["End"].notna() & (df["End"] <= cutoff)]
    return df
```

Everything else (your cost pipeline, receipts, etc.) remains unchanged.

---

#### C. Authentication: swap dummy DB verification for **PAM**

> Rationale: Slurm itself doesn’t verify user passwords for web apps; it expects system accounts / directory. Keep your web login aligned with cluster login by using PAM (or your SSO that also backs the cluster). `slurmrestd` auth controls access to the REST daemon; PAM controls your web session.

Minimal change in `controllers/auth.py`:

```python
# controllers/auth.py
# 1) add dependency: pip install python-pam
import pam

def _pam_auth(username: str, password: str) -> bool:
    p = pam.pam()
    # The service name can be 'login' or a custom /etc/pam.d/<service>
    return bool(p.authenticate(username, password, service=os.environ.get("PAM_SERVICE", "login")))

# in login_post(), replace:
#   if not verify_password(u, p):
# with:
if not _pam_auth(u, p):
    # ... keep your audit + throttle as-is
    ...
```

**Admin role determination options** (pick one):

- **Static list** (env/DB): keep your current role field and just migrate user creation to PAM-backed identities.
- **Query Slurm AdminLevel**: on login, run `sacctmgr show user where user=<u> format=User,AdminLevel` (or use slurmrestd `/slurmdb/*` if exposed). Treat `AdminLevel=Operator` or `Admin` as app-admin (read-only vs full). (SlurmDBD AdminLevel & PrivateData are documented in `slurmdbd.conf`.) ([Debian Manpages][2])

---

### 4) Security & operations

- **JWT handling**: Obtain JWT via `scontrol token` (user context or privileged issuer) and present it to `slurmrestd`. Configure `AuthAltTypes=auth/jwt` on the cluster and secure secrets; the manpage and `slurm.conf` docs explain JWT usage. ([Slurm][1], [Debian Manpages][2])
- **Visibility and least privilege**:

  - For the admin who fetches “all users” via `sacct`, prefer **AdminLevel=Operator** (read-only). ([Debian Manpages][2])
  - Be aware `PrivateData` can limit visibility of other users’ jobs; coordinate with cluster admins if outputs look sparse. (Documented under SlurmDBD/Slurm config.) ([Debian Manpages][2])

- **TLS**: Put `slurmrestd` behind HTTPS. If you must test with self-signed certs, set `SLURMRESTD_VERIFY_TLS=0` only in dev.
- **Fallback**: Keep your `sacct` fallback; it aligns with Slurm’s own tools. Supported flags are in the man page (`-S/-E/--format/--parsable2`). ([Slurm][1])

---

### 5) Environment variables (app)

- `SLURMRESTD_URL` — e.g. `https://slurm-ctl.example:6820`
- `SLURMRESTD_TOKEN` — (optional) JWT string for the calling user
- `SLURMRESTD_VERSION` — e.g. `v0.0.39` (match your cluster) ([Slurm][1])
- `SLURMRESTD_VERIFY_TLS` — `1` (default) / `0` (dev)
- `PAM_SERVICE` — PAM stack name to use (default `login`)

---

### 6) Quick test runbook

1. **REST up?**

   - From a node that can reach the daemon:

     ```bash
     export SLURM_JWT=$(scontrol token)   # as the test user
     curl -s --fail \
       -H "X-SLURM-USER-TOKEN: $SLURM_JWT" \
       "$SLURMRESTD_URL/slurm/v0.0.39/jobs?start_time=2025-01-01T00:00:00&end_time=2025-12-31T23:59:59" | jq .
     ```

     (Header/cookie names are in the slurmrestd man page.) ([Slurm][1])

2. **App fetch**

   - Set `SLURMRESTD_URL` (and `SLURMRESTD_TOKEN` for your user).
   - Hit your app’s Usage page; confirm `data_source=slurmrestd`.

3. **Admin visibility**

   - If admin needs all users via `sacct`, ensure AdminLevel is set (Operator/Admin) and that relevant `PrivateData` settings don’t hide data; re-run. ([Debian Manpages][2])

---

### 7) Optional: using `/slurmdb` endpoints

If you prefer pulling **historical accounting** directly through REST (instead of CLI `sacct`), enable the **slurmdb OpenAPI plugin** in `slurmrestd`. The docs show `openapi` plugins (including `slurmdb`), and you can query paths under `/slurmdb/v…`. Use the same JWT token mechanism.

---

### References

- **slurmrestd man page** — auth (JWT), header/cookie names, examples. ([Slurm][1])
- **sacct man page** — flags/formatting for accounting data. ([Slurm][1])
- **slurmrestd OpenAPI plugins** — includes `slurmdb` for accounting.
- **slurm.conf (Debian manpage)** — JWT configuration (`AuthAltTypes=auth/jwt`, token notes). ([Debian Manpages][2])
- **slurmdbd.conf (SchedMD)** — `AdminLevel`, `PrivateData`, accounting visibility/roles. ([Debian Manpages][2])

---

### TL;DR hand-off

- **You only edit one place** inside the app’s data flow: `fetch_from_slurmrestd()` in `services/data_sources.py` → call the new `services/slurmrest_client.query_jobs()`.
- **Login**: replace `verify_password()` with PAM (`python-pam`).
- **Cluster**: enable `slurmrestd` + JWT, ensure accounting and AdminLevel for operators.

That’s it — the rest of the pipeline (costs, receipts, admin tables) continues to work unchanged.

[1]: https://slurm.schedmd.com/slurmrestd.html "Slurm Workload Manager - slurmrestd"
[2]: https://manpages.debian.org/experimental/slurm-client/slurm.conf.5 "slurm.conf(5) — slurm-client — Debian experimental — Debian Manpages"

## Features:

### UI & UX

- **Global shell**

  - Animated blue/purple **wavy background** with reduced-motion fallback.
  - Centered **site panel** (cards/tables/forms styling, sidebar nav, tabs, chips).
  - **Header/nav** (left/right groups, logo/brand).
  - **Favicon/logo** wired so a tiny icon shows on every tab.
  - **i18n** (Flask-Babel) with `/i18n/set` to switch language; cookie respected.

- **Auth screens**

  - Login form with **full-width button** and inline **error/status message** (no flash).
  - CSRF tokens on forms; custom CSRF error page.

- **Playground**

  - **HPC Cost Playground** that fetches `/formula?type=…`, shows current per-hour rates, live recalculation & breakdown.

- **User pages**

  - **/me** “My Usage”: filter by “completed before”, **detail/aggregate/billed** tabs.
  - **Create Receipt** (server filters out already-billed), **CSV download**, receipt list & receipt detail pages.

- **Admin pages**

  - Sidebar sections: **Change Rate**, **Usage Tables (all users)**, **My Usage (admin’s own)**, **Billing**, **Audit**.
  - **Change Rate** for `mu|gov|private` tiers.
  - **Usage Tables** (detail + aggregate across users) with totals.
  - **My Usage** mirrors user flow (detail/aggregate/billed), **Create Receipt** for self, **my.csv** export.
  - **Billing**: list **pending** receipts, **Mark as paid**, **paid.csv** export.
  - **Audit**: recent audit table and **audit.csv** export.

### API & Services

- **Rates API**

  - `GET /formula?type=tier` → current rates (THB, per-hour).
  - `POST /formula` (admin-only) → update tier rates.

- **Data ingestion**

  - `data_sources.fetch_jobs_with_fallbacks(start,end, username?)` with cascade:

    1. **slurmrestd** client hook (standalone helper; configurable)
    2. **sacct** CLI (`--parsable2`, end-time cutoff, optional user filter)
    3. **test.csv** fallback (path configurable)

  - Uniform DataFrame with `User, JobID, Elapsed, TotalCPU, ReqTRES, End, State`.
  - **Cost computation** via `services.billing.compute_costs` (adds CPU/GPU/MEM hours, tier, Cost (฿)).

- **Billing store**

  - SQLite models with **receipts** and **receipt_items** (UNIQUE job_key to prevent double-billing).
  - Helpers: `create_receipt_from_rows`, `list_receipts`, `get_receipt_with_items`,
    `billed_job_ids`, `canonical_job_id`, `list_billed_items_for_user`,
    admin helpers (`admin_list_receipts`, `mark_receipt_paid`, `paid_receipts_csv`).

### Auth & Security

- **Login/Logout** with Flask-Login; roles (`admin|user`) and `admin_required` decorator.
- **CSRF** protection (Flask-WTF) + global `csrf_token()` in Jinja.
- **Temporary lockout / throttling**

  - SQLite **auth_throttle** table.
  - Configurable: `AUTH_THROTTLE_MAX_FAILS`, `AUTH_THROTTLE_WINDOW_SEC`, `AUTH_THROTTLE_LOCK_SEC`.
  - **Inline messages** for “locked” and “invalid credentials”.

- **Route hygiene**

  - Admin blocked from `/me` (redirect), admins land on **/playground** not `/me`.
  - Login always redirects to **/playground** (ignore `next`).

### Auditing & Observability

- **Tamper-evident audit log** (hash-chained entries) with indexes.
- Context captured: timestamp (UTC), actor, IP, UA, method, path, action, target, status, extra, prev_hash, hash.
- **Audited actions** (examples already wired):

  - `auth.login.success|failure|lockout.start|lockout.active|lockout.end|logout`
  - `rates.update.form`
  - `receipt.paid`
  - (If you added earlier as discussed) `receipt.create` for user/admin

- **Export & viewing**: `/admin/audit` (last N), `/admin/audit.csv`.
- **App logging**: rotating file logs, per-request timing via before/after request.

### Config & Ops

- **Instance path** setup, SQLite initializers for billing/users/audit/throttle.
- **Admin seeding** (ENV `ADMIN_PASSWORD`); optional demo users via `SEED_DEMO_USERS`.
- **Env-driven** file paths (DB, fallback CSV), slurmrestd URL/token (via helper).
- **Internationalization** defaults (`en`, `th`).

### Little QoL touches

- Totals chips (CPU/GPU/MEM/Elapsed), grand total badges.
- CSV exports for user/admin usage and paid receipts.
- Responsive header/grid, accessible animations, aria-live login status.
