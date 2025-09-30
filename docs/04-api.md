# API (Overview)

This chapter explains our public/admin HTTP endpoints, the auth/CSRF model, ETag usage, and where to find the **OpenAPI** spec used by Swagger UI. The API surface is intentionally small; most admin workflows are UI‑first.

> **Status:** Online payments webhook is **not exposed** in this build. Admin marks receipts paid manually.

---

## 1) Auth model & security schemes

- **Authentication:** Flask‑Login session cookie (`session`). Obtain it by signing in via the web UI.
- **Authorization:** Role‑based (`admin` / `user`). Admin‑only endpoints require the admin cookie.
- **CSRF:** Session‑backed **POST** requests require header `X-CSRFToken` (except explicitly exempt endpoints like `/copilot/ask`).
- **ETag:** `GET /formula` supports strong ETags and `If-None-Match` for efficient polling.

```yaml
components:
  securitySchemes:
    cookieAuth:
      type: apiKey
      in: cookie
      name: session
    xsrfToken:
      type: apiKey
      in: header
      name: X-CSRFToken
```

---

## 2) Endpoint matrix

| Path                          | Method | Auth     | CSRF    | Content                | Notes                                         |
| ----------------------------- | ------ | -------- | ------- | ---------------------- | --------------------------------------------- |
| `/healthz`                    | GET    | none     | no      | JSON                   | Liveness                                      |
| `/readyz`                     | GET    | none     | no      | JSON                   | DB reachability                               |
| `/metrics`                    | GET    | none     | no      | text/plain             | Prometheus exposition                         |
| `/formula`                    | GET    | optional | no      | JSON                   | Current tier rates; supports **ETag** and 304 |
| `/formula`                    | POST   | admin    | **yes** | JSON                   | Update one or many tiers                      |
| `/admin/audit.verify.json`    | GET    | admin    | no      | JSON                   | Verify HMAC chain (tamper‑evidence)           |
| `/admin/ledger.csv`           | GET    | admin    | no      | text/csv               | **Derived** journal export for a window       |
| `/admin/export/ledger.csv`    | GET    | admin    | no      | text/csv               | **Posted** GL export                          |
| `/admin/export/gl/formal.zip` | POST   | admin    | **yes** | application/zip        | Bundle of posted GL + manifest + HMAC         |
| `/admin/simulate_rates.json`  | GET    | admin    | no      | JSON                   | Pricing sandbox for charts                    |
| `/admin/forecast.json`        | GET    | admin    | no      | JSON                   | Forecast series for charts                    |
| `/copilot/widget.js`          | GET    | none     | no      | application/javascript | Embeddable widget (if enabled)                |
| `/copilot/ask`                | POST   | none     | **no**  | JSON                   | CSRF‑exempt Q&A (rate‑limited)                |
| `/copilot/reindex`            | POST   | admin    | **yes** | JSON                   | Rebuild docs index                            |

> CSV endpoints stream files; Swagger UI will show them but cannot preview large files.

---

## 3) Using ETag with `/formula`

```bash
# 1) Initial fetch
curl -i http://localhost:8000/formula
# HTTP/1.1 200 OK
# ETag: "\"5bd2779c...\""

# 2) Conditional fetch (polling without changes)
curl -i \
  -H 'If-None-Match: "\"5bd2779c...\""' \
  http://localhost:8000/formula
# HTTP/1.1 304 Not Modified
```

**Response body (200):**

```json
{
  "version": "2025-09-01T00:00:00Z",
  "tiers": [
    {
      "tier": "mu",
      "cpu": 0.02,
      "gpu": 1.5,
      "mem": 0.001,
      "updated_at": "2025-09-01T00:00:00Z"
    },
    {
      "tier": "gov",
      "cpu": 0.03,
      "gpu": 2.0,
      "mem": 0.002,
      "updated_at": "2025-09-01T00:00:00Z"
    },
    {
      "tier": "private",
      "cpu": 0.05,
      "gpu": 3.5,
      "mem": 0.003,
      "updated_at": "2025-09-01T00:00:00Z"
    }
  ]
}
```

---

## 4) Updating rates (admin)

**Single tier**

```bash
curl -X POST http://localhost:8000/formula \
  -H 'Content-Type: application/json' \
  -H 'X-CSRFToken: <token>' \
  -H 'Cookie: session=<admin-session>' \
  -d '{"tier":"gov","cpu":3.5,"gpu":10.0,"mem":1.0}'
```

**Bulk**

```bash
curl -X POST http://localhost:8000/formula \
  -H 'Content-Type: application/json' \
  -H 'X-CSRFToken: <token>' \
  -H 'Cookie: session=<admin-session>' \
  -d '{"tiers":[{"tier":"mu","cpu":0.02,"gpu":1.50,"mem":0.001}]}'
```

The server accepts **either** a single‑tier body or a `tiers[]` array. Invalid or non‑admin sessions receive 401/403 with a Problem JSON.

---

## 5) Audit verification

```bash
curl -s http://localhost:8000/admin/audit.verify.json | jq
# {
#   "ok": true,
#   "checked": 1245,
#   "break_index": null
# }
```

If the chain is broken, you’ll see `ok=false` and the first mismatch index.

---

## 6) Copilot endpoints (optional)

- `GET /copilot/widget.js` – injects the chat widget.
- `POST /copilot/ask` – JSON `{ "q": "…" }` → `{ "answer": "…", "tokens": …, "sources": [...] }`; CSRF‑exempt; rate‑limited per minute.
- `POST /copilot/reindex` – admin only; rebuilds the vector index from docs.

---

## 7) Swagger UI & OpenAPI

Swagger UI is included in the dev stack. To update the spec:

1. Edit `docs/api/openapi.yaml` (see updated version below).
2. The **Swagger** container serves it at `http://localhost:8081/`.

---

## 8) Errors

All JSON errors follow a _Problem_ shape:

```json
{ "error": "Forbidden", "code": 403, "detail": "admin required" }
```

---

## 9) Updated OpenAPI

See **`docs/api/openapi.yaml`** (kept in version control).
