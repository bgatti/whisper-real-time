# Deployment

The `noise/web` app deploys to Railway from a local checkout via the Railway CLI. There is no GitHub-triggered CI — `railway up` packages and uploads the source directly.

## Quick reference

```bash
# From the repo root (NOT noise/web — see "Why --path-as-root")
cd c:/Users/Benja/OneDrive/Documents/whipser_real_time

# Deploy
railway up -d --path-as-root -m "<commit-style message>" noise/web

# Watch status
railway deployment list | head -3

# Tail logs (latest deploy)
railway logs --latest -n 50

# Tail runtime logs (currently-serving deploy)
railway logs -n 50

# Show deploy URL / domain
railway domain
```

- **Production**: <https://web-app-production-fedf.up.railway.app>
- **Project**: `aviation-monitor` · **Service**: `web-app` · **Environment**: `production`
- **Database**: Postgres on Railway (`DATABASE_URL` env var injected automatically)

## Why `--path-as-root noise/web`

The Railway service was provisioned without a "root directory" set, so `railway up` from the repo root uploads everything (Python scripts, `.venv`, etc.) and Railpack can't figure out the build. The fix is `--path-as-root noise/web` — this tells Railway to treat `noise/web/` as the project root for build/run.

Running `railway up` from inside `noise/web/` does **not** work either — the CLI errors with "prefix not found" because Railway expects the deploy archive to be relative to a project root.

So always:
```bash
# right
cd <repo-root>
railway up -d --path-as-root -m "..." noise/web

# wrong (uploads whole repo)
railway up -d -m "..."

# wrong (CLI rejects)
cd noise/web && railway up
```

Run **all** `railway` commands from the repo root (`railway status`, `railway deployment list`, `railway logs`). Run from `noise/web/` or any other subdir and the CLI reports `No linked project found` — the link metadata lives at the repo root.

## What gets shipped

`railway up` packages the **current `noise/web` working tree on disk** — *not* `git HEAD`. Uncommitted edits, new untracked files, and unsaved IDE changes all go up. Conversely, a clean commit that hasn't been written to disk in this checkout (e.g. you committed on a different machine) will **not** be deployed by running `railway up` here.

Practical consequences:
- "I committed it but prod doesn't show the change" usually means a deploy was run from a different checkout that didn't have your edits on disk. Re-run `railway up` from the checkout that actually contains the file.
- Conversely, half-finished local work in `noise/web/` *will* ship if you `railway up` — `git status` what's in the tree before deploying anything non-trivial.
- The nested `noise/web/.git` is independent of the outer repo (`noise/web/` is gitignored in the outer repo). Commits to either are deploy-orthogonal.

## Deploy lifecycle

A `railway up` produces one deployment that goes through these states:

1. `BUILDING` — Railpack runs `npm ci` then `npm run build`. Most failures happen here (missing imports, syntax errors). Local `npm run build` catches these before you upload.
2. `DEPLOYING` — image is uploaded and the new container starts. Old container keeps serving traffic until health check passes.
3. `SUCCESS` — new container is live, old one is torn down.
4. `FAILED` / `CRASHED` — old deploy continues serving. Read `railway logs --latest` to see why.

Build cache: subsequent deploys reuse the `node_modules` layer when `package-lock.json` is unchanged, so most deploys are 30–90s. A `package.json` change forces a full reinstall (~3 min).

## Pre-deploy checks

Run these locally before `railway up` to fail fast:

```bash
cd noise/web
npm run build                           # catches import errors, syntax, type issues
npx vitest run                          # unit tests (no server needed)
NOISE_BASE=http://localhost:5174 npx vitest run noiseReports.test.js   # against local dev
```

Don't deploy if `npm run build` fails — Railpack will fail with the same error at the `BUILDING` stage and burn ~2 minutes per failed attempt.

## Verifying a deploy

```bash
BASE="https://web-app-production-fedf.up.railway.app"

# Smoke test — should return JSON, not the SPA HTML fallback
curl -s "$BASE/api/adsb/config/fleet" | head -c 200
curl -s "$BASE/api/noise-zones" -o /dev/null -w "HTTP %{http_code}\n"

# Run integration tests against prod
cd noise/web
npx vitest run noiseReports.test.js     # default base is prod
```

If a JSON endpoint returns `<!doctype html>`, the new deploy isn't live yet (or the route was renamed). Check `railway deployment list` — only the row marked `SUCCESS` is serving.

**Verifying a client/SPA change (e.g. a new route like `/kiosk`).** Prod serves via `vite` in dev mode, so it transforms `/src/*.jsx` on the fly. Don't rely on the route's HTTP status or content-type — the SPA fallback returns `200 text/html` for *any* path (`/kiosk`, `/src/Nonexistent.jsx`, etc.), so those always "pass". Instead grep the served source module for a symbol that only the new code has:

```bash
BASE="https://web-app-production-fedf.up.railway.app"
curl -s "$BASE/src/App.jsx" | grep -c KioskMap        # >0 → new route wired in App
curl -s "$BASE/src/KioskMap.jsx" | grep -c ContourLayer # >0 → new module is live
```

**Verifying an API change.** Pick a response field that exists *only* in the new code — usually a new param echo or a new shape field — and assert it's present. HTTP 200 alone doesn't prove the new code shipped (an old endpoint silently ignores unknown params). E.g. for the leaderboard `homeBase` filter added on 2026-05-31:

```bash
BASE="https://web-app-production-fedf.up.railway.app"
curl -s "$BASE/api/noise/leaderboard?by=tail&homeBase=KBDU&limit=1" \
  | python -c "import sys,json; d=json.load(sys.stdin); print('home_base echo:', d.get('home_base','<ABSENT>'))"
# '<ABSENT>' → old code still serving; 'KBDU' → new code live.
```

This same pattern works as a client-side **version detector** for slowly-rolling consumers (the kiosk uses `home_base` echo to auto-flip from a synthetic board to the real API).

## Rollback

```bash
railway deployment list                 # find the last known-good ID
railway redeploy                        # redeploys the most recent SUCCESS deploy
# or, in the dashboard, click the older deploy → "Redeploy"
```

`railway down` removes the most recent deployment (Railway falls back to the previous one). Useful for quick reverts when a bad deploy slipped through.

## Watching logs during a deploy

`railway logs --latest` follows the *building/deploying* deployment. `railway logs` without `--latest` follows the *currently serving* deployment. After a successful deploy, both point at the same thing.

For long builds, run the deploy detached (`-d`) and poll instead of streaming attached logs. Concrete loop that exits on a terminal state (observed deploy on 2026-05-31 went `BUILDING` ×11 → `DEPLOYING` → `SUCCESS` in ~130 s):

```bash
cd <repo-root>
railway up -d --path-as-root -m "..." noise/web

for i in $(seq 1 30); do
  STATUS=$(railway deployment list 2>/dev/null \
    | head -3 \
    | grep -oE 'BUILDING|DEPLOYING|SUCCESS|FAILED|CRASHED|QUEUED|INITIALIZING' \
    | head -1)
  echo "[t+$((i*10))s] $STATUS"
  case "$STATUS" in SUCCESS|FAILED|CRASHED) break;; esac
  sleep 10
done
railway deployment list | head -5     # final ID + status
```

`grep -oE` on the deployment-list header row is more reliable than column-position parsing — the table format has shifted across CLI versions. The 30 × 10 s cap (5 min) covers normal builds plus a slow npm install; bump it for `package.json` changes.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Railpack could not determine how to build the app` | Forgot `--path-as-root noise/web`; uploaded whole repo | Re-run with the flag |
| `BUILDING` → `FAILED` with import error | A file imports something that doesn't exist | Run `npm run build` locally first |
| Endpoint returns HTML, not JSON | New deploy isn't live yet, or route registered after the SPA fallback | Check `railway deployment list`; verify plugin order in `vite.config.js` |
| `Pool error: ... timeout` in logs | Postgres connection storm on cold start | Usually self-recovers; if not, restart with `railway restart` |
| Out-of-memory crash on Railway | Vite preview server holding too much in process memory | `start` script in package.json uses `--max-old-space-size=4096` — bump if needed |
| `Build Failed: ... ResourceExhausted: ... no space left on device` (`/var/lib/buildkit/...`) | Railway **builder node** is out of disk — platform infra, not your code/config. Fails while pulling `railpack-frontend` before `npm ci` even runs. | **Not fixable from our side.** Forcing `NIXPACKS` via `railway.json` fails identically (and mis-detects this app as a static Caddy site — don't). Just retry; it clears on Railway's side, often within hours (cleared overnight for us). Note: a successful deploy of a *different* Railway project ≠ this builder is healthy — projects land on different builders. |

## Environment variables

```bash
railway vars                            # list all
railway vars --set KEY=value            # set one
railway vars --kv | grep NOISE_         # filter
```

Service-relevant vars:
- `DATABASE_URL` — auto-injected by Railway when Postgres plugin is attached
- `RAILWAY_ENVIRONMENT=production` — read by `vite.config.js` to disable HMR
- `PORT=5174` — must match the port Vite binds to in `start` script
- `NOISE_NOTICE_SECRET` — HMAC key for pilot-response tokens (set; do not log)
- `RESEND_API_KEY` — email send (set; absent = dry-run mode)
