# Deploy Guide (agent rooted in `noise/`)

The deployable app lives in `web/`. Production runs on **Railway**, project
`aviation-monitor`, served from Postgres. The Railway CLI is already
authenticated on this machine — no login step needed.

- **Live site:** https://web-app-production-fedf.up.railway.app
- **Project:** `db36698b-470a-41b8-86ab-72bfe3f18af0`
- **Environment:** `production` (`8f6958b1-21cf-46d8-a927-872e33a4d8a7`)

## Services

| Service | ID | Start command | Source |
|---------|-----|---------------|--------|
| `web-app` | `63ec9d81-a956-453c-a2ce-aa91a145c6d0` | `vite --host 0.0.0.0 --mode production` | `web/` |
| `capture-worker` | `8719b35f-3bf4-4ee0-9b48-44829b08440c` | `node capture-worker.js` | `web/` |
| Postgres | — | managed | — |

Both services deploy from the **same `web/` directory** but run different
start commands. `DATABASE_URL` is already set as an env var on each service —
do not pass it at deploy time.

## Why a clean copy, not `railway up` from `web/`

`web/.git` exists but `git archive` is unreliable here (OneDrive path quirks),
and uploading `node_modules` (~280 MB) or `dist/` bloats the upload. The
reliable recipe is: copy `web/` to a scratch dir, strip `.git`/`dist`, then
`railway up`. `.railwayignore` (contains `node_modules`) keeps the upload small.

## Deploy `web-app` (the website)

```bash
# from the noise/ root
rm -rf /c/tmp/railway-deploy && mkdir -p /c/tmp/railway-deploy
cp -r web/. /c/tmp/railway-deploy/
rm -rf /c/tmp/railway-deploy/.git /c/tmp/railway-deploy/dist

cd /c/tmp/railway-deploy
railway up --service web-app \
  --project db36698b-470a-41b8-86ab-72bfe3f18af0 \
  --environment production
```

`web/package.json`'s `start` script is already
`vite --host 0.0.0.0 --mode production`, so no edit needed for web-app.

Build takes ~50–90s. Verify:

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
  https://web-app-production-fedf.up.railway.app/api/noise/years
```

## Deploy `capture-worker` (live ADS-B ingest)

The worker needs `start` = `node capture-worker.js`. Nixpacks honors
`package.json`'s `start` over env-var overrides, so you must rewrite it in the
scratch copy before uploading:

```bash
rm -rf /c/tmp/cw-deploy && mkdir -p /c/tmp/cw-deploy
cp -r web/. /c/tmp/cw-deploy/
rm -rf /c/tmp/cw-deploy/.git /c/tmp/cw-deploy/dist

# rewrite start script (use forward-slash Windows path for node on this box)
node -e "const p=JSON.parse(require('fs').readFileSync('C:/tmp/cw-deploy/package.json'));p.scripts.start='node capture-worker.js';require('fs').writeFileSync('C:/tmp/cw-deploy/package.json',JSON.stringify(p,null,2))"

cd /c/tmp/cw-deploy
railway up --service capture-worker \
  --project db36698b-470a-41b8-86ab-72bfe3f18af0 \
  --environment production
```

Verify it started with the right entrypoint (must show `node capture-worker.js`,
NOT `vite`):

```bash
railway service capture-worker      # link, run from noise/ or web/
railway logs                        # expect: [capture-worker] starting...
```

## Common gotchas

- **Always pass `--project` and `--environment` explicitly.** `railway up`
  from a scratch dir has no link context and otherwise picks the wrong service.
- **Upload timeouts** happen occasionally — just re-run the same `railway up`.
- **Stale browser bundle:** after a web-app deploy, hard-refresh
  (Ctrl+Shift+R). The JS bundle is cache-busted by build hash, but the HTML
  shell can cache.
- **`useServerApi` must stay `true`** in `web/src/App.jsx`. If it regresses to
  `useState(false)`, fresh page loads download all ~50K tracks instead of the
  filtered ~500 from the API. It has regressed 3×; keep it a hard `const`.
- **Don't commit/push for a deploy** — `railway up` uploads the working copy
  directly. Commit only when you want history.

## Data files that ride along

These must exist under `web/public/` so they land in Railway's `/app/public/`:

- `special_use_aircraft.json` — enriches `/api/excursions/flight-ops` with
  medevac/firefighting/military/etc. purposes. Read via
  `path.resolve('public/special_use_aircraft.json')` (cwd is `/app` on Railway,
  `web/` locally).

## Verifying excursions live data

```bash
# live tracks classified at capture time (bands + worst per track)
curl -s "https://web-app-production-fedf.up.railway.app/api/excursions/boot?hours=1&limit=100" \
  | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{const j=JSON.parse(d);console.log('tracks',j.tracks.length,'live',j.live)})"
```

See `web/API.md` for the full endpoint reference.
