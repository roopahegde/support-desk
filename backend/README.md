# Support Desk – Backend API

The REST API behind the Support Desk agent. It owns the data (customers, products, orders,
refunds) and **enforces the business rules**, so every client gets the same answers no matter
who calls it. The MCP servers call this API. The agent never talks to it directly.

```
Agent  →  MCP servers  →  this REST API (FastAPI)  →  SQLite
```

**Stack:** Python, FastAPI, Pydantic, SQLite (Python's built-in `sqlite3`), pytest, ruff.

---

## 1. Prerequisites

| Tool | Version | Check with |
|---|---|---|
| Python | 3.10 or newer (CI uses 3.13) | `python --version` |
| git | any recent version | `git --version` |

Run every command in this guide **from the repository root**, the folder that contains
`backend/` and `pyproject.toml`, not from inside `backend/`.

## 2. Set up

### Get the code and create a virtual environment
```bash
git clone https://github.com/roopahegde/support-desk.git
cd support-desk
python -m venv .venv
```

Activate it:

| Shell | Command |
|---|---|
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

Your prompt now starts with `(.venv)`. If PowerShell refuses to run the script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and call
`.venv\Scripts\python.exe` directly.

### Install the dependencies
```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
```
`requirements.txt` holds what the API needs to run. `requirements-dev.txt` adds the test and
lint tools. Both are pinned to exact versions.

### Create your `.env`
```bash
# Windows
copy backend\.env.example backend\.env
# macOS / Linux
cp backend/.env.example backend/.env
```
Then open `backend/.env` and set `BACKEND_API_KEYS`: one `actor=key` pair per client,
separated by commas. Give every client its own long random key. Generate each one with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
For example: `BACKEND_API_KEYS=agent:support-desk=<key 1>,staff:yourname=<key 2>`. Use your
`staff:` key for curl and Swagger. The MCP servers use the `agent:` key.
`backend/.env` is ignored by git. **Never commit it, and never paste the key into code or chat.**

### Create the database
```bash
python -m backend.app.seed
```
This creates `backend/support_desk.db` with the scenario data described in
[section 5](#5-seed-data-the-test-scenarios). Run it again at any time to **reset** the data.
The dates are calculated relative to the moment you seed, so re-seed if the data is more than a
few days old.

## 3. Run the API

```bash
python -m uvicorn backend.app.main:app --reload
```

- **Swagger UI:** http://127.0.0.1:8000/docs. Click **Authorize**, paste your API key, then
  use **Try it out** on any endpoint.
- **`--reload`** restarts the server when a `.py` file changes. It does **not** reload
  `backend/.env`, so restart the server by hand after editing that file.
- **Optional `--no-access-log`:** hides uvicorn's own one-line access log, since the API
  already logs each request in more detail.

Quick check from a second terminal (on Windows, type `curl.exe`, because `curl` is a
PowerShell alias for something else):
```bash
curl.exe -H "X-API-Key: <your key>" http://127.0.0.1:8000/orders/1042
```

## 4. Configuration

Every setting lives in `backend/.env`, and `backend/.env.example` documents them. Real
environment variables take priority over the file, which is how CI and production set them.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BACKEND_API_KEYS` | **yes** | – | One `actor=key` pair per client, comma-separated. A client sends its key in the `X-API-Key` header, and the API knows it by its actor name (for example `agent:support-desk`), which the audit trail records. If the setting is missing, empty or malformed, every request fails with `500 server_misconfigured`, and the log says what is wrong without printing a key. |
| `LOG_TARGET` | no | `console` | `console` or `file` |
| `LOG_FILE` | no | `logs/backend.log` | Used when `LOG_TARGET=file`. A relative path is relative to `backend/`. The file rotates at 5 MB and keeps 5 old files. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

Log lines are plain text with `key=value` fields. Every line carries the request's ID, which
is also returned to the client in the `X-Request-ID` response header:
```
2026-11-11 15:03:44 WARNING [req=634db0245513] backend.app.errors: request_failed status=422 code=policy_violation message="Order 1043 was delivered 45 days ago. ..."
```
API keys, headers and request bodies are never logged.

## 5. Seed data (the test scenarios)

| Order | Customer | Status | Delivered | Total (INR) | Scenario it covers |
|---|---|---|---|---|---|
| 1042 | 1 | delivered | 5 days ago | 3,499 | Happy path: a damaged-item refund succeeds |
| 1043 | 1 | delivered | 45 days ago | 2,199 | Outside the 30-day refund window |
| 1044 | 2 | delivered | 10 days ago | 54,999 | Above INR 10,000, so it needs human approval |
| 1045 | 2 | refunded | 14 days ago | 1,299 | Already refunded (idempotency key `seed-refund-1045`) |
| 1046 | 3 | delivered | 3 days ago | 1,899 | Contains the out-of-stock kettle (replacement case) |
| 1047 | 3 | shipped | – | 2,499 | Not delivered yet: can't be refunded or cancelled |
| 1048 | 1 | placed | – | 2,598 | Not shipped: can be cancelled |
| 1049 | 2 | delivered | 20 days ago | 2,499 | `damaged` is accepted, `changed_mind` is not (over 7 days) |

The product `KTL-500` (Electric Kettle) has **0 stock**.

## 6. API overview

Every endpoint needs the `X-API-Key` header. `/docs` and `/openapi.json` are public.
Money is always in **paise** (1 INR = 100 paise), so INR 3,499 is `349900`.

| Method | Path | What it does |
|---|---|---|
| GET | `/orders/{id}` | One order with its line items |
| GET | `/orders/{id}/events` | The order's history, oldest first: each change, who made it (`actor`), on whose behalf (as stated, not verified), when, and the request ID |
| GET | `/customers/{id}/orders` | A customer's orders, newest first (`[]` if they have none) |
| GET | `/products/{sku}/stock` | Stock level. `0` is a valid answer, not an error. |
| GET | `/policies/refund` | The refund policy the API enforces |
| GET | `/orders/{id}/refund-eligibility?reason=damaged&amount_paise=349900` | Would this refund be allowed right now? Answers `eligible`, `needs_approval` or `not_eligible` (with the code and message the refund would get). Creates nothing. "No" is a `200`, not an error. |
| POST | `/orders/{id}/refunds` | Create a refund. Needs an `Idempotency-Key` header and a body like `{"reason": "damaged", "amount_paise": 349900}` |
| POST | `/orders/{id}/cancel` | Cancel an order that has not shipped |

### Refund rules
- Only **delivered** orders, within **30 days** of delivery.
- Reasons: `damaged`, `wrong_item`, `not_as_described`, and `changed_mind` (only within **7 days**).
- The amount must be above zero and no more than the order total.
- Above **INR 10,000**, the refund is saved as `pending_approval` and the order is not marked
  refunded. (There is no approval endpoint yet.)
- **One refund per order.** Sending the same `Idempotency-Key` again returns the original
  refund instead of creating a second one, so clients can retry safely after a timeout.

### Order history (audit trail)
- Every refund and cancellation adds one row to the `order_events` table, in the **same
  transaction** as the change itself, so there is never a change without its record (or a record
  without its change). Retries and repeated cancels change nothing, so they add nothing.
- **Who** is the client whose API key was used. **On behalf of** comes from the optional
  `X-On-Behalf-Of` header (for example `customer:1`, up to 100 characters). It is recorded as
  stated and is **not verified**.
- The table is append-only: database triggers reject any `UPDATE` or `DELETE`. Only the seed
  script, which resets test data, drops and recreates it.
- Seeded orders come with their history (placed, shipped, delivered, refunded) from the actor
  `system:seed`.
- Refused requests (`409`, `422`) are not history: they appear in the log only.

### Cancellation rules
- Only orders that are still **placed** (not shipped) can be cancelled.
- Cancelling an order that is already cancelled changes nothing and returns it, so retries are safe.
- Shipped, delivered and refunded orders are refused with `422 policy_violation`, and the
  message says what the customer can do instead (for example, request a refund after delivery).
- There is no time limit, value limit or reason. Cancelling only changes the order's status: payments
  are not modelled. These rules are not published by an endpoint (only the refund policy is).

### Errors
Every error, including validation errors and crashes, has the same shape:
```json
{"error": {"code": "policy_violation", "message": "Order 1043 was delivered 45 days ago. Refunds are only allowed within 30 days of delivery."}}
```
Use `code` in program logic, and show `message` to people (or to an AI agent).
Validation errors also include a `fields` list.

| Status | Codes |
|---|---|
| 401 | `unauthorized` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `already_refunded`, `refund_pending_approval`, `idempotency_key_reused` |
| 422 | `validation_error` (malformed request), `policy_violation` (a business rule says no) |
| 500 | `server_misconfigured`, `internal_error` |

## 7. Tests and linting

```bash
ruff check backend                    # lint
pytest backend/tests                  # all tests
pytest backend/tests -v               # one line per test
pytest backend/tests/test_rules.py    # one file
pytest backend/tests --cov=backend.app --cov-report=term-missing   # with coverage
```

- The tests need **no running server, no network and no `.env`**. Each test builds its own
  temporary database and sets its own API key, so your `support_desk.db` is never touched.
- The test files are `test_seed.py` (scenario data), `test_rules.py` (business rules as plain
  functions), `test_api.py` (every endpoint through FastAPI's `TestClient`) and
  `test_logging.py` (log output and what must never be logged).
- The same tests are described in plain English, for reviewers and testers, in
  [tests/TEST_CASES.md](tests/TEST_CASES.md).

**CI:** `.github/workflows/backend.yml` runs ruff and pytest on GitHub on every push and pull
request that changes `backend/`. The test and coverage reports are saved with each run.

## 8. Project layout

```
backend/
  app/
    main.py            # endpoints, API-key check, request logging
    rules.py           # business rules (refunds, cancellation) as plain functions
    policy.py          # the refund policy values
    models.py          # request and response models (the API contract)
    errors.py          # the single error format and its handlers
    logging_config.py  # where logs go: the one place to switch console or file
    db.py              # SQLite schema and connection
    seed.py            # scenario data; run it to reset the database
  tests/               # pytest suites, plus TEST_CASES.md
  requirements.txt     # runtime dependencies
  requirements-dev.txt # test and lint tools
  .env.example         # settings template (copy to .env)
```

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every request returns `500 server_misconfigured` | `BACKEND_API_KEYS` is missing, empty or malformed. The server log names the bad entry. Fix it in `backend/.env`, then restart the server. If you used the old single `BACKEND_API_KEY` setting, rename it and add an actor: `BACKEND_API_KEYS=staff:yourname=<your key>`. |
| `401 unauthorized` | The `X-API-Key` header is missing, or isn't one of the keys in `BACKEND_API_KEYS`. In Swagger, click **Authorize**. |
| A refund on order 1042 says it was delivered over 30 days ago | The seed data is old. Re-run `python -m backend.app.seed`. |
| `[Errno 10048]` or "address already in use" | Another server is already on port 8000. Stop it, or use `--port 8001`. |
| `ModuleNotFoundError: No module named 'backend'` | You ran the command from inside `backend/`. Run it from the repository root. |
| Changes to `backend/.env` have no effect | `--reload` only watches `.py` files. Restart the server. |
| On Windows, `python -m venv` fails with an `ensurepip` error, or `pip install` fails with `ModuleNotFoundError: No module named 'pip._vendor...'` | The folder path is too long: Windows limits paths to 260 characters by default, and some pip files sit deep inside `.venv`. Clone into a shorter folder (for example `C:\dev\support-desk`), or [enable long paths](https://learn.microsoft.com/windows/win32/fileio/maximum-file-path-limitation) in Windows. |

## 10. Not included yet

- An approval workflow for `pending_approval` refunds (listing them, then approving or rejecting them).
- Ownership checks: any client with a valid key can read or change any customer's orders.
- Production concerns: a server database such as PostgreSQL with migrations, a secrets manager
  instead of `.env`, HTTPS and rate limiting.
