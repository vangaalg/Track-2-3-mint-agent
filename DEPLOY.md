# Deploy — OI/macro recorder on Railway (always-on)

Run `feeds.recorder` 24×5 on Railway so the 15-min OI + macro accumulation never depends on
a local terminal staying open. One web service hosts a **daily-token endpoint** + a status page
and runs the recorder + a git-sync loop in background threads. Data persists by committing to a
**private data repo** (Railway disks are ephemeral).

## 0. One-time prep
1. **Create a private GitHub repo** for the data, e.g. `vangaalg/mint-data` (empty is fine).
2. **Make a GitHub Personal Access Token** (fine-grained, *Contents: read/write* on that repo).
3. Build the data-repo URL with the token:
   `https://<PAT>@github.com/vangaalg/mint-data.git`

## 1. Create the Railway service
- New Project → **Deploy from GitHub repo** → pick this repo (branch `claude/dazzling-lamport-7d0je8`).
- Railway auto-detects Python (nixpacks) and uses the `Procfile`:
  `web: uvicorn web.recorder_service:app --host 0.0.0.0 --port ${PORT:-8000}`
- It exposes a public HTTPS URL (you'll POST the token to it from your phone).

## 2. Set environment variables (Railway → Variables)
| Variable | Value |
|---|---|
| `BREEZE_API_KEY` | your Breeze app key |
| `BREEZE_API_SECRET` | your Breeze secret |
| `BREEZE_SESSION_TOKEN` | today's token (you'll refresh it daily via the page) |
| `TWELVEDATA_API_KEY` | your Twelve Data key (macro; optional) |
| `RECORDER_TOKEN_SECRET` | a password you choose — guards `POST /token` |
| `DATA_REPO_URL` | `https://<PAT>@github.com/vangaalg/mint-data.git` |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | any name/email for commits |
| `RECORDER_INSTRUMENTS` | optional, e.g. `NIFTY,BANKNIFTY` (default = enabled defaults) |
| `RECORDER_STOCKS` | optional, `1` to also record the Nifty-50 stocks (hourly) |
| `INDEX_EVERY_MIN` / `STOCK_EVERY_MIN` | optional, default `15` / `60` |
| `SYNC_EVERY_MIN` | optional, git-push cadence, default `30` |

## 3. Daily routine (≈30 seconds, from your phone)
1. Generate today's Breeze session token (ICICI Breeze login → API session).
2. Open the Railway service URL → paste the token + your `RECORDER_TOKEN_SECRET` → **Update token**.
3. The page shows `breeze: connected` if the token is valid. The recorder picks it up on its
   next 15-min cycle — no restart. The token is also persisted to the data repo, so a container
   **restart/redeploy auto-restores it** (`/healthz` shows `token_restored`) — you only re-POST
   once a day for a fresh token, not after every redeploy.
4. **(Optional) Morning overlay** — on the same page, the *GIFT + events* form: enter the manual
   **GIFT Nifty** level (overrides the best-effort investing.com auto-fetch — it's the source of
   truth when investing.com blocks the server) and paste the **overnight-events note** (the
   geopolitical brief Claude gave you from a screenshot). Saved via `POST /context` (same secret);
   GIFT lands in `data/macro/macro.parquet` as `gift_nifty_*`, the note in `data/context.json`.

## 4. Verify it's working
- Service URL status page shows `saved: ['NIFTY', 'BANKNIFTY']`, `macro: true`, a recent
  `last cycle`, and `last push` advancing every ~30 min.
- `GET /healthz` returns the same as JSON (for an uptime monitor).
- Your private data repo receives commits during the session.

## 5. Pull the data locally to analyze
```cmd
git clone https://github.com/vangaalg/mint-data.git
```
Then in this repo, point the stores at it (or copy `data/` over) and:
```python
from feeds.oi_summary_store import load_summary
load_summary("NIFTY")   # growing PCR / max-pain / walls / bands time series
```

---

# Deploy — Cockpit (the live trading UI) on Railway

Reach the cockpit from anywhere (phone/laptop) instead of `localhost:8000`. It runs as a
**second Railway service** from this same repo, isolated from the recorder so a cockpit
redeploy never disrupts the recording flywheel. It is **password-protected** (HTTP Basic
over Railway's TLS), inherits the daily Breeze token from the recorder's shared data repo,
and **persists its decision journal + learning memory** to a private repo.

> Vercel/Netlify won't work — the cockpit is a long-running stateful server (in-memory
> caches, the gated trigger queue, background pulls), not serverless functions.

## 1. Create the service
- Same Railway project → **New Service → Deploy from GitHub repo** → this repo, branch
  `claude/dazzling-lamport-7d0je8`.
- **Settings → Deploy → Custom Start Command:**
  `uvicorn web.cockpit_service:app --host 0.0.0.0 --port $PORT`
  (the recorder keeps the default `Procfile`; only this service overrides it).

## 2. Environment variables (Railway → Variables)
| Variable | Value |
|---|---|
| `COCKPIT_USER` / `COCKPIT_PASSWORD` | the login for the cockpit (you choose) — **required**, fail-closed |
| `ANTHROPIC_API_KEY` | for Claude's read/sparring |
| `BREEZE_API_KEY` / `BREEZE_API_SECRET` | your Breeze app creds |
| `TWELVEDATA_API_KEY` | macro scorecard (optional) |
| `RECORDER_TOKEN_SECRET` | guards the fallback `POST /token` |
| `DATA_REPO_URL` | **the SAME** private data repo the recorder uses (read replica → inherits the token + OI store) |
| `JOURNAL_REPO_URL` | a **separate** private repo for the journal, `https://<PAT>@github.com/vangaalg/mint-journal.git` |
| `RECORDER_URL` | the recorder service's public HTTPS URL (so the cockpit's token form can forward today's token to it — single entry point) |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | any name/email for commits |
| `SYNC_EVERY_MIN` | optional, journal push cadence, default `30` |

> **One PAT, both repos:** you can use a SINGLE fine-grained PAT for `DATA_REPO_URL` and
> `JOURNAL_REPO_URL` — give it *Contents: read/write* on both private repos (or scope it to
> "All repositories") and reuse the same token string in both URLs.

## 3. Use it
- Open the service's HTTPS URL → the browser prompts for `COCKPIT_USER`/`COCKPIT_PASSWORD`
  → the cockpit loads. `/healthz` stays open (for an uptime monitor); `/cockpit-status`
  shows sync state + a token fallback form.
- **Token (primary, in-app):** tap the **🔑 Token** button in the cockpit header → paste
  today's Breeze session token → **Save**. No secret needed (you're already logged in). It
  applies the token to the cockpit AND forwards it to the recorder (via `RECORDER_URL`), so
  this is the *one* place you refresh the token — the separate recorder phone page is now an
  optional fallback. The form auto-reveals (amber button) whenever the feed looks
  unauthenticated. The response shows `cockpit: connected · recorder: ok`.
- **Token (fallbacks):** the cockpit also still pulls the shared data repo (~every 10 min),
  so a token POSTed to the recorder is inherited automatically; `/cockpit-status` keeps the
  secret-guarded form too.
- **Journal:** every approve/reject commits to `JOURNAL_REPO_URL` (plus a periodic push),
  so your track record + Claude's memory survive redeploys. `git clone` it to analyze.

## Notes / risks
- **Token is manual by design** — Breeze has no refresh API. If a morning is missed, that day
  isn't recorded (logged, non-fatal); just post the token when you can.
- **Breeze from a Railway IP** — verify the first `connected` probe. If Breeze blocks the cloud
  IP, fall back to a small always-on box you control (same Procfile/command).
- **Data-loss window** ≤ `SYNC_EVERY_MIN` (default 30 min) if the container dies between pushes.
- **Secrets in the data repo** — API key/secret live only in env, never the repo. The one
  exception is the **daily Breeze session token**: it's persisted under `data/recorder_state/`
  so it survives restarts (a deliberate tradeoff — the token expires daily and the data repo is
  private). Everything else in the repo is parquet.
