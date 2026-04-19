# Deployment — Wildfire Watch

End-to-end deploy: CDK stacks for the backend, AWS Amplify for the React
frontend. Hand-off doc — anyone with AWS credentials can run this.

## Prereqs

- AWS CLI configured against the target account (`aws sts get-caller-identity` should return the right account)
- Node 20+ and Python 3.11+
- AWS CDK v2 (`npm i -g aws-cdk`)
- A Mapbox public token (starts with `pk.`) — grab from mapbox.com if we don't have one already
- The GitHub repo (`A-M-05/wildfire-watch`) accessible by the AWS account doing the Amplify connect

## Step 1 — Deploy the CDK stacks

Bootstrap once per account/region (skip if already done):

```bash
cd infrastructure
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap aws://<ACCOUNT_ID>/us-west-2
```

Deploy everything:

```bash
cdk deploy --all --require-approval never
```

Takes ~10-15 min on a cold account. When it finishes, **save the stack
outputs** — you'll need three values for the frontend:

- `WildfireWatchFrontend.RestApiUrl` → set as `VITE_API_URL`
- `WildfireWatchFrontend.WebSocketUrl` → set as `VITE_WS_URL`
- `WildfireWatchFrontend.AmplifyAppId` → used in step 2

If you missed them, re-run:

```bash
aws cloudformation describe-stacks \
  --stack-name WildfireWatchFrontend \
  --query 'Stacks[0].Outputs'
```

## Step 2 — Connect the GitHub repo to Amplify

The CDK creates an empty Amplify shell. Repo connection has to happen in
the console (CDK would need a GitHub OAuth token in Secrets Manager,
overkill for hackathon).

1. Open the Amplify console → pick the `wildfire-watch-frontend` app (id from step 1)
2. **Hosting environments** → **Connect a branch**
3. Authorize GitHub if prompted, pick `A-M-05/wildfire-watch`, branch `main`
4. **Build settings** — Amplify will detect `amplify.yml` at the repo root.
   Confirm it's using that file (monorepo config, app root `frontend`,
   build output `frontend/dist`). No edits needed.
5. **Skip** the IAM service role page — default Amplify role is fine.

## Step 3 — Set frontend env vars

Same Amplify app → **Hosting** → **Environment variables**. Add three:

| Key | Value |
|---|---|
| `VITE_MAPBOX_TOKEN` | `pk.…` (Mapbox public token) |
| `VITE_API_URL` | the `RestApiUrl` from step 1 |
| `VITE_WS_URL` | the `WebSocketUrl` from step 1 |

Leave `VITE_USE_MOCK_FIRES` unset (defaults to live data). Set it to
`true` if the backend pipeline isn't producing fires yet and you need to
demo against `public/data/active_fires.geojson`.

## Step 4 — Trigger the first build

After saving env vars, hit **Run job** on the `main` branch. First build
takes ~3-4 min (npm ci + vite build). Output URL is the
`<branch>.<app-id>.amplifyapp.com` link in the console.

## Step 5 — Smoke test

1. Open the Amplify URL — Mapbox basemap should render
2. Wait ~3-5s — fire polygons should appear (live from CAL FIRE via the
   pipeline, or from the mock GeoJSON if `VITE_USE_MOCK_FIRES=true`)
3. Click a fire — dispatch panel slides in on the right, green dotted
   tethers animate from each dispatched station to the fire
4. Red Cross shelter dots are visible across CA at all zoom levels
5. Reservoir dots are clickable, pop up the drought severity chip

If any of those don't work, check the browser console for env-var
errors (most likely culprit: missing `VITE_API_URL` or `VITE_MAPBOX_TOKEN`).

## Tearing it down

```bash
cd infrastructure && cdk destroy --all
```

The Amplify app + its connected branch are deleted with the stack. The
DynamoDB tables have `RemovalPolicy.DESTROY` set so they're wiped too —
fine for hackathon, would change for prod.

## Things to know

- **Backend pipeline takes time to populate fires.** CAL FIRE poller runs every 10 min via EventBridge. First fire data lands ~10 min after deploy. Use mock mode (`VITE_USE_MOCK_FIRES=true`) for any demo within the first hour.
- **Confidence gate at 0.65** is hardcoded in the safety Lambda — anything below routes to manual review. Don't lower this without team consensus (per CLAUDE.md safety contract).
- **SCP blocks Pinpoint and QLDB.** Per-resident SMS uses `sns.publish(PhoneNumber=...)` direct, audit ledger uses DynamoDB hash-chain. Don't try to add Pinpoint or QLDB resources — they'll fail at deploy.
- **WebSocket routes are stubs** until issue #30 lands. Map will fall back to 30s polling, which is fine for the demo.
