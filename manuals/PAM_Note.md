import time
import os
import pam # python-pam
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
import jwt # PyJWT
Short answer first: **slurmrestd does not authenticate users with PAM directly.** It supports two request-side auth plugins:

- `rest_auth/local` (trusts the ** Unix peer identity ** over a local UNIX socket)
- `rest_auth/jwt` (trusts ** JSON Web Tokens ** sent with the request)

See the slurmrestd man page(options `-a rest_auth/local` and `-a rest_auth/jwt`, header use, and examples). ([openSUSE Manpages][1])

The way to use PAM with slurmrestd is to put PAM ** in front of ** slurmrestd and have that service ** mint JWTs ** that Slurm accepts. Slurm’s JWT plugin is designed for this: you can configure Slurm to accept HS256(shared key) or RS256(JWKS public key) tokens, and you can choose which claim carries the UNIX user name(`userclaimfield`). ([Slurm][2], [docs.rackslab.io][3])

Below is a practical, copy-pasteable manual for a ** PAM→JWT token broker ** + slurmrestd, plus exactly where our Flask app needs a small edit to start using it.

---

# Manual — Slurm integration using **PAM** (via a PAM→JWT token broker)

# What you’ll build

1. A tiny HTTPS microservice on a trusted node that:

- checks username/password with **PAM**, and
- issues a short-lived ** JWT ** containing the user name.

2. Configure Slurm(**slurmctld ** & **slurmrestd**) to ** validate those JWTs**.

3. Teach our Flask app to attach:

```
X-SLURM-USER-NAME: < username >
X-SLURM-USER-TOKEN: < JWT >
```

on every slurmrestd call. (These are the standard headers slurmrestd expects for JWT auth.)([openSUSE Manpages][1])

---

# Requirements / Assumptions

- Slurm with **slurmrestd ** available(21.08 + recommended).
- You can modify ** slurm.conf ** / service units and restart slurmctld.
- A place to run the broker with PAM enabled(e.g., login node or API node).
- TLS certificates for the broker ( and optionally for an HTTPS reverse proxy in front of slurmrestd).
- A shared ** HS256 secret ** ( or a ** JWKS ** file for RS256) configured in Slurm so it can validate JWTs. (Slurm documents `jwt_key=` for HS256 and `jwks=` for RS256.) ([Slurm][2])

---

# Step 1 — Configure Slurm to trust JWTs

On the controller, enable the JWT auth plugin and provide a key:

**slurm.conf**

```ini
# Allow REST clients to authenticate with JSON Web Tokens
AuthAltTypes = auth/jwt
# Choose *one* of the following:
# For shared-secret HS256:
AuthAltParameters = jwt_key = /etc/slurm/jwt_hs256.key, userclaimfield = preferred_username
# or for RS256 with a JWKS public key set:
# AuthAltParameters=jwks=/etc/slurm/jwks.json,userclaimfield=preferred_username
```

Notes:

- `jwt_key = `: path to HS256 secret(0400, owned by SlurmUser/root).
- `jwks = `: path to a JSON Web Key Set with **public ** keys(0400).
- `userclaimfield = `: which JWT claim contains the ** UNIX user name ** Slurm should run as (e.g., `preferred_username`, `sub`, or `username`). Slurm’s JWT docs describe these parameters and external token use. ([Slurm][2], [docs.rackslab.io][3])

Restart `slurmctld` to pick up changes(per standard Slurm practice). ([niflheim-system.readthedocs.io][4])

---

# Step 2 — Run **slurmrestd** with JWT auth

Run slurmrestd using the JWT auth plugin:

- Systemd drop-in (example):

```
# /etc/systemd/system/slurmrestd.service.d/override.conf
[Service]
ExecStart =
ExecStart = /usr/sbin/slurmrestd - a rest_auth/jwt - s v0.0.41 - d
```

(`-s` selects the REST API version to expose; use what your cluster supports.)

The slurmrestd man page covers `-a rest_auth/jwt` and shows that clients send `X-SLURM-USER-NAME` and `X-SLURM-USER-TOKEN` (JWT) with each request. ([openSUSE Manpages][1])

> Tip: bind slurmrestd on an internal interface, put NGINX/Apache in front for TLS and rate-limiting.

---

# Step 3 — Create a minimal **PAM→JWT token broker**

This is a small HTTPS service that:

- checks user/password with PAM,
- signs a JWT containing that user name(using the same HS256 secret or an RS256 private key corresponding to your `jwks`), and
- returns the token to the client.

**/etc/pam.d/slurmrest-web ** (example; adjust to your environment)

```
auth    required pam_unix.so
account required pam_unix.so
# or via SSSD / LDAP as appropriate
# auth    required pam_sss.so
# account required pam_sss.so
```

**Python FastAPI example(use gunicorn/uvicorn behind HTTPS)**

```python
# broker.py

JWT_ALG = os.getenv("JWT_ALG", "HS256")  # HS256 or RS256
JWT_KEY = open(
    os.getenv("JWT_KEY_PATH", "/etc/slurm/jwt_hs256.key"), "rb").read()
JWT_EXP = int(os.getenv("JWT_EXP_SECONDS", "900"))  # 15 min default
USER_CLAIM = os.getenv("SLURM_USER_CLAIM", "preferred_username")

pam_auth = pam.pam()
app = FastAPI()


class Login(BaseModel):
    username: str
    password: str


@app.post("/token")
def token(body: Login):
    if not pam_auth.authenticate(body.username, body.password, service="slurmrest-web"):
        raise HTTPException(status_code=401, detail="invalid credentials")

    now = int(time.time())
    claims = {
        USER_CLAIM: body.username,
        "iat": now,
        "exp": now + JWT_EXP,
        "iss": "pam-jwt-broker",
        "aud": "slurm",
    }
    tok = jwt.encode(claims, JWT_KEY, algorithm=JWT_ALG)
    # PyJWT returns str for HS256; bytes for RS256 in some versions — normalize:
    tok = tok if isinstance(tok, str) else tok.decode("utf-8")
    return {"token": tok, "expires_in": JWT_EXP}


```

- If using ** RS256**, point `JWT_KEY_PATH` to your ** private key**, and in Slurm configure `jwks = /etc/slurm/jwks.json` with the ** public ** key. ([Slurm][2])
- If using ** HS256**, share `/etc/slurm/jwt_hs256.key` (readable by Slurm daemons and the broker, root-owned 0400). ([Slurm][2])

---

# Step 4 — How **our Flask app** should use the token

1. On login(to ** our ** web app), call the broker’s `/token` with the user’s credentials, store the returned {username, token, expires_at} in the Flask session (server-side).

2. When calling slurmrestd, attach:

```
X-SLURM-USER-NAME:  <username>
X-SLURM-USER-TOKEN: <JWT>
```

This is exactly how JWT auth works in slurmrestd. ([openSUSE Manpages][1])

### The only code change we need in our repo

Create a small helper and swap one function in `services/data_sources.py`.

**New file: `services/slurm_rest_client.py`**

```python
# services/slurm_rest_client.py
import os, requests
from typing import Optional

class SlurmREST:
    def __init__(self, base_url: str, username: str, token: str, timeout: int = 10):
        self.base = base_url.rstrip("/")
        self.username = username
        self.token = token
        self.timeout = timeout

    def _headers(self):
        return {
            "X-SLURM-USER-NAME": self.username,
            "X-SLURM-USER-TOKEN": self.token,
        }

    def jobs(self, start_iso: str, end_iso: str) -> dict:
        url = f"{self.base}/slurm/v0.0.39/jobs"
        params = {"start_time": start_iso, "end_time": end_iso}
        r = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
```

**Edit `services/data_sources.py` — replace the stub `fetch_from_slurmrestd`**

```python
# inside data_sources.py
import pandas as pd
from .slurm_rest_client import SlurmREST
from flask import session  # if you store token there

def fetch_from_slurmrestd(start_date: str, end_date: str, username: str | None = None) -> pd.DataFrame:
    """
    Fetch via slurmrestd (JWT auth). Requires app to have stored 'slurm_username' and 'slurm_token'.
    """
    base = os.environ.get("SLURMRESTD_URL") or "http://slurmctld:6820"  # set in env/config
    # pull from Flask session (or however your login code stored it)
    u = username or session.get("slurm_username")
    t = session.get("slurm_token")
    if not (u and t):
        raise RuntimeError("missing slurm JWT in session")

    cli = SlurmREST(base, u, t)
    js = cli.jobs(f"{start_date}T00:00:00", f"{end_date}T23:59:59")

    rows = []
    for j in js.get("jobs", []):
        user   = j.get("user_name") or j.get("user")
        jobid  = j.get("job_id")    or j.get("jobid")
        elapsed_s  = j.get("elapsed") or j.get("time", {}).get("elapsed")
        totalcpu_s = j.get("stats", {}).get("total_cpu")
        tres_str   = j.get("tres_req_str") or j.get("tres_req") or j.get("tres_fmt") or ""
        rows.append({
            "User":    user,
            "JobID":   jobid,
            "Elapsed": elapsed_s if isinstance(elapsed_s, str) else sec_to_hms(elapsed_s or 0),
            "TotalCPU": totalcpu_s if isinstance(totalcpu_s, str) else sec_to_hms(totalcpu_s or 0),
            "ReqTRES": tres_str,
        })
    if not rows:
        raise RuntimeError("slurmrestd returned no jobs")
    return pd.DataFrame(rows)
```

> With this in place, **everything else in our app keeps working** (costing, receipts, admin pages), because they already consume a pandas DataFrame from `fetch_jobs_with_fallbacks()`.

---

## Step 5 — Client flow & quick tests

- **Get a token** from the broker:

```bash
curl -sS -X POST https://broker.example.org/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"••••"}'
# -> {"token":"eyJhbGciOi...","expires_in":900}
```

- **Call slurmrestd** using that token (and the same username):

```bash
curl -sS http://slurm-api.local:6820/slurm/v0.0.39/jobs \
  -H "X-SLURM-USER-NAME: alice" \
  -H "X-SLURM-USER-TOKEN: $TOKEN"
```

Man page shows the same header usage and a similar curl pattern. ([openSUSE Manpages][1])

---

## Security notes (strongly recommended)

- Keep the broker **inside the HPC network**; expose it only to your web app or identity tier. Use **TLS** everywhere.
- Use **short JWT lifetimes** (e.g., 10–15 minutes) and renew on demand.
- If you use HS256, keep `jwt_key` secret tightly controlled (0400). For multi-service setups, prefer **RS256/JWKS** so Slurm only needs the **public** key(s). ([Slurm][2])
- Rate-limit the broker; audit issuance (username, IP, success/failure).
- slurmrestd should run with **JWT auth** and live behind a reverse proxy with TLS and basic DoS protection. (Man page documents auth modes and encourages secured deployments.) ([openSUSE Manpages][1])

---

## Why not “PAM directly in slurmrestd”?

Because slurmrestd’s published **auth types** are `rest_auth/local` and `rest_auth/jwt`—there is no `rest_auth/pam`. JWT is the supported way to authenticate remote HTTP clients. ([openSUSE Manpages][1])

---

## References

- **slurmrestd(8) man page** — authentication types `rest_auth/local` and `rest_auth/jwt`, header usage with `X-SLURM-USER-NAME` and `X-SLURM-USER-TOKEN`, and curl examples. ([openSUSE Manpages][1])
- **Slurm JSON Web Tokens** — configuring JWT keys (`jwks=` for RS256 public keys, `jwt_key=` for HS256 shared key) and claim mapping (`userclaimfield`) so external tokens are accepted. ([docs.rackslab.io][3])
- **scontrol(1)** — documents token generation / JWT integration from the Slurm side (handy when testing locally).
- **slurmdbd.conf(5)** (JWT parameters are described identically to slurm.conf; shows `jwks=` and `jwt_key=` semantics and file protections). ([Slurm][2])

---

## TL;DR for the implementing team

1. **Enable JWT** in Slurm (`AuthAltTypes=auth/jwt`, set `jwt_key=` or `jwks=` + `userclaimfield=`). ([docs.rackslab.io][3], [Slurm][2])
2. Run **slurmrestd** with `-a rest_auth/jwt`. ([openSUSE Manpages][1])
3. Deploy a small **PAM→JWT broker** (sample above), using PAM stack `/etc/pam.d/slurmrest-web`.
4. In our Flask app, **drop in** `services/slurm_rest_client.py` and replace `fetch_from_slurmrestd()` exactly as shown.
5. On login, call the broker to get a JWT, store it, and the rest of our pipeline (usage → costing → receipts) will “just work” off the same DataFrame flow.

[1]: https://manpages.opensuse.org/Tumbleweed/slurm-rest/slurmrestd.8.en.html?utm_source=chatgpt.com "slurmrestd(8) — slurm-rest"
[2]: https://slurm.schedmd.com/slurmdbd.conf.html?utm_source=chatgpt.com "slurmdbd.conf - Slurm Workload Manager - SchedMD"
[3]: https://docs.rackslab.io/slurm-web/install/update.html?utm_source=chatgpt.com "Update - Slurm-web - Rackslab Documentations"
[4]: https://niflheim-system.readthedocs.io/en/latest/Slurm_configuration.html?utm_source=chatgpt.com "Slurm configuration — Niflheim 24.07 documentation"

---

Here’s a quick, practical checklist to verify **PAM is present and usable on your cluster** (login/head nodes and wherever you’ll run the PAM→JWT broker). Run the commands on a representative node with sudo/root.

# 1) Does the PAM framework exist?

```bash
# PAM config directory?
test -d /etc/pam.d && echo "✓ /etc/pam.d exists" || echo "✗ /etc/pam.d missing"

# Show some PAM service files
ls -l /etc/pam.d | head

# Core PAM library present?
ldconfig -p | grep -E 'libpam\.so' || echo "✗ libpam not found in ldconfig cache"
```

# 2) Are PAM packages installed?

- **RHEL/CentOS/Rocky/Alma/SLES**:

```bash
rpm -q pam pam-libs || echo "✗ pam not installed (rpm)"
```

- **Debian/Ubuntu**:

```bash
dpkg -l | grep -E '^ii\s+(libpam0g|libpam-modules|libpam-modules-bin)' || echo "✗ pam not installed (dpkg)"
```

# 3) Is at least one PAM service in use?

SSH typically uses PAM:

```bash
grep -i '^UsePAM' /etc/ssh/sshd_config || echo "Note: no explicit UsePAM line"
grep -R "pam_unix\.so" /etc/pam.d | head
```

# 4) Will your **broker**-specific PAM service exist?

(We suggested using a dedicated stack like `/etc/pam.d/slurmrest-web`.)

```bash
# Does the service file exist?
test -f /etc/pam.d/slurmrest-web && echo "✓ /etc/pam.d/slurmrest-web found" || echo "✗ create /etc/pam.d/slurmrest-web"

# Peek inside (should reference pam_unix.so or pam_sss.so etc.)
sed -n '1,120p' /etc/pam.d/slurmrest-web 2>/dev/null || true
```

# 5) Can PAM actually authenticate a user?

Use a non-interactive tester:

- **Install tester** (if available in your distro):

  - RHEL-family: `yum install -y pamtester`
  - Debian/Ubuntu: `apt-get install -y libpam-test pamtester` (package names vary)

- **Test** (you’ll be prompted for the password):

```bash
pamtester slurmrest-web <username> authenticate
echo $?
# exit code 0 = success, non-zero = failure
```

(If `pamtester` isn’t available, you can temporarily point the service to the same rules as `sshd` or test via the Python snippet below.)

# 6) If you’ll use the Python broker, is the **python-PAM** binding available?

```bash
python3 - <<'PY'
try:
    import pam  # python3-pam
    print("✓ python-pam installed")
except Exception as e:
    print("✗ python-pam missing:", e)
PY
```

- If missing, install:

  - RHEL-family: `yum install -y python3-pam`
  - Debian/Ubuntu: `apt-get install -y python3-pam`

# 7) (Optional) Quick programmatic check with Python

This verifies PAM can authenticate through your chosen service:

```bash
python3 - <<'PY'
import getpass
import pam
svc = "slurmrest-web"
u   = input("Username: ")
p   = getpass.getpass("Password: ")
ok = pam.pam().authenticate(u, p, service=svc)
print("Result:", "OK" if ok else "FAIL")
PY
```

# 8) Fan-out checks (many nodes)

- **pdsh**:

```bash
pdsh -a 'test -d /etc/pam.d && echo OK || echo NO_PAM'
pdsh -a 'ldconfig -p | grep -q libpam.so && echo LIB_OK || echo NO_LIB'
```

- **Ansible** (minimal play):

```yaml
- hosts: all
  gather_facts: no
  tasks:
    - stat: path=/etc/pam.d
      register: pamd
    - name: Fail if PAM missing
      fail: msg="/etc/pam.d not found"
      when: not pamd.stat.exists
```

---

## Interpreting results

- If `/etc/pam.d` exists and `libpam.so.*` is present, **PAM is installed**.
- If the `slurmrest-web` file doesn’t exist, **create it** (e.g., using `pam_unix.so` or `pam_sss.so` depending on local vs LDAP/SSSD).
- If authentication fails with `pamtester` or the Python snippet, check your PAM rules (e.g., `/etc/pam.d/slurmrest-web`), NSS/SSSD config, and that the user actually exists (`getent passwd <user>`).

If you want, send me the outputs (redact usernames), and I’ll pinpoint what’s missing.
