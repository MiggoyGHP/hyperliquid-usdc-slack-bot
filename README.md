# Hyperliquid USDC Utilization → Slack

Posts the utilization rate of Hyperliquid's native USDC borrow/lend market to Slack
every two hours, with spot BTC and ETH alongside it for context and a louder alert as
utilization approaches and crosses the 80% rate kink.

## Why 80% matters

Hyperliquid's published stablecoin borrow curve is:

```
borrow_apy = 0.05 + 4.75 * max(0, utilization - 0.80)
```

The rate is pinned at a 5% floor until utilization hits **80%**, then climbs steeply.
That kink — not the raw drift — is the number worth being woken up about.

## Data source

No API key, no wallet, no RPC. One public endpoint:

```bash
curl -s https://api.hyperliquid.xyz/info \
  -H 'Content-Type: application/json' \
  -d '{"type":"borrowLendReserveState","token":0}'
```

Token `0` is USDC. Every numeric field returns as a **string**, so the bot parses
straight to `Decimal` — never through `float`.

## Behaviour

Two independent rules. Cloud Scheduler ticks every two hours; the bot decides whether
that tick is worth a message.

**Cadence — how often it posts**

| Utilization | Posts every |
|---|---|
| any | 2 hours |

In the deployed job the cron *is* the cadence — see
[Cadence: `HEARTBEAT_HOURS=0`](#cadence-heartbeat_hours0-in-the-deployed-job). Elsewhere,
set `HEARTBEAT_HOURS` higher for a quieter channel; `ESCALATE_AT` then switches to
`ESCALATED_INTERVAL_HOURS` above that utilization. With both set to 1 (the default) the
escalation rule is inert and every tick posts.

**Spot majors — context, not a rule.** Each posted message carries the BTC and ETH spot
mids captured on that tick, read from Hyperliquid's own `BTC/USDC` and `ETH/USDC` markets
(`UBTC`/`UETH` against USDC). They never influence whether a tick posts. The read happens
only on a tick that will post, and a failure costs the two fields rather than the message.

**Severity band — how loud the post is.** A band *change* posts immediately,
regardless of cadence.

| Band | Enters at | Colour |
|---|---|---|
| `NORMAL` | < 80% | green |
| `HIGH` | ≥ 80% | amber |
| `CRITICAL` | ≥ 90% | red |

**Hysteresis (1.5 points, exit only).** Leaving `HIGH` needs < 78.5%; leaving
`CRITICAL` needs < 88.5%. Without it, a reading hovering at 80.00% would alert on
every single tick. Note 78.5% sits below the 79% cadence trigger by design —
cadence calms down before the band does, never the reverse.

## Layout

```
hl_usdc_bot/
  hyperliquid.py  read the reserve state + spot majors; Decimal parsing, validation
  rates.py        borrow curve, headroom to the kink, $ and % formatting
  bands.py        severity bands + hysteresis
  decide.py       PURE: (reading, prev state, now, config) -> Decision
  state.py        BotState + LocalFileStateStore / GcsStateStore
  slack.py        Block Kit message + webhook delivery
  runner.py       one tick: read -> decide -> post -> remember
  app.py          Flask: POST /tick, GET /healthz
  tick_once.py    same tick, from the command line
  config.py       environment -> frozen Config
wsgi.py           gunicorn entrypoint (unused by the deployed job)
deploy/
  setup.ps1       provision the Cloud Run job + two-hourly trigger, idempotent
  set-webhook.ps1 write/rotate the webhook in Secret Manager
  env.yaml        non-secret job configuration
```

`decide.py` does no I/O — no clock, no network, no disk. That is what makes the
whole cadence-by-band matrix testable directly (`tests/test_decide.py`).

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Run a real tick against the live market without touching Slack:

```powershell
$env:DRY_RUN="1"; $env:STATE_FILE=".\state.json"
.\.venv\Scripts\python.exe -m hl_usdc_bot.tick_once
```

It prints the exact Slack payload it would send. Run it twice — the second run
should report `SUPPRESSED`, proving the cadence guard works (`HEARTBEAT_HOURS`,
one hour by default). Delete `state.json` to start over.

The second run also proves the spot-price read is skipped when nothing will be posted.

## Deploying on Google Cloud

A **Cloud Run Job** triggered every two hours by **Cloud Scheduler**, with the webhook in
**Secret Manager** and the bot's state in a **Cloud Storage** bucket.

A Job rather than a Service because `tick_once.py` is already a batch entrypoint that runs
and exits, so no HTTP wrapper is needed — and because Scheduler's `:run` call returns the
moment the execution is enqueued. Nothing holds a connection open for the length of the
tick, so a slow Hyperliquid read can never trip a scheduler deadline and provoke a retry
that posts twice. That matters more here than it would for a stateless bot: this one
mutates dedupe state, so a duplicate run is a duplicate alert. `app.py` and `wsgi.py`
remain in the repository and still work — see [Running elsewhere](#running-elsewhere) — but
nothing in the deployed path uses them.

### Why this replaced GitHub Actions

GitHub runs scheduled workflows **on a best-effort basis**: individual runs are delayed
15–60 minutes under load and are occasionally dropped outright, and nothing you can put in
a workflow makes a given run punctual. The workflow's answer was to poll six times an hour
so that at least one attempt would beat the queue. It was not enough. The tick commits in
this repository's own history are the record:

```
16:30 → 19:47 → 22:28 → 00:45 UTC     gaps of 3h17m, 2h41m, 2h17m against a 1h target
```

Cloud Scheduler fires within a few seconds of the hour, so a single poll per interval now
does what six could not.

### 1. Slack webhook

api.slack.com/apps → **Create New App** → **From scratch** → name it and pick the workspace
→ set the name and icon under **Basic Information → Display Information** (the payload
cannot override these) → **Incoming Webhooks** → toggle **On** → **Add New Webhook to
Workspace** → choose the channel → copy the `https://hooks.slack.com/services/...` URL.

### 2. Prerequisites

Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), then:

```powershell
gcloud init
gcloud auth login
```

If a shell that was already open cannot see `gcloud` after installing it, that is the shell
holding a pre-install copy of `PATH`. VSCode's integrated terminals inherit the editor's
environment from when it launched, so a *new* terminal is not enough there — the editor
itself has to restart. `setup.ps1` sidesteps both by re-reading `PATH` from the registry,
so it runs fine in a stale shell.

The project needs **billing enabled** — Cloud Build and Artifact Registry refuse to run
without it. Actual spend is nil; see [Cost](#cost).

Docker is *not* needed. `--source` hands the build to Cloud Build.

### 3. Provision

Copy the webhook URL to the clipboard, then:

```powershell
.\deploy\setup.ps1 -Project <your-project-id> -DryRun -SeedState
```

It enables the six required APIs, reads the webhook from the clipboard and stores it in
Secret Manager, creates two service accounts and the state bucket, builds and deploys the
job, and creates the two-hourly trigger. It is idempotent — re-run it after a failure or a
code change.

`-DryRun` deploys with `DRY_RUN=1`, so the job renders the payload into Cloud Logging and
posts nothing. `-SeedState` uploads the repository's `state.json` to the bucket, but only
if the bucket holds none — that carries `last_band` across the migration so the first tick
does not re-announce a crossing the Actions run already sent.

Check it:

```powershell
gcloud run jobs execute hl-usdc-tick --region asia-southeast1 --wait
```

Read the payload and the `utilization=… band=… reason=… post=…` line in the logs. Note that
a dry run still writes state — only the Slack post is skipped — which makes it a real test
of the bucket wiring:

```powershell
gcloud storage cat gs://<your-project-id>-hl-usdc-state/state.json
```

A fresh `last_post_ts` there means GCS state is working. When the payload looks right, go
live:

```powershell
.\deploy\setup.ps1 -Project <your-project-id>
gcloud run jobs execute hl-usdc-tick --region asia-southeast1 --wait
```

That last execution should put exactly one message in the channel.

At the original cutover this was preceded by `gh workflow disable "Hyperliquid USDC
utilization"`. Running both hosts at once means two bots against two different state
stores, neither aware of the other's posts — so if the workflow is ever restored,
disable it again before going live here.

### Two service accounts, not one

`hl-usdc-job` runs the container and may read the secret and the state bucket.
`hl-usdc-scheduler` may invoke the job and nothing else. Splitting them means the trigger —
the component reachable from outside — cannot read the webhook.

The webhook is never in the image, in `deploy/env.yaml`, or in a tracked file.
`.dockerignore` excludes `.env` for the same reason: image layers are readable by anyone
with pull access. `setup.ps1` asserts both of the things that fail silently here, and
refuses to finish if either is wrong:

```powershell
gcloud run jobs describe hl-usdc-tick --region asia-southeast1
```

`SLACK_WEBHOOK_URL` must appear as a secret reference, never as a literal, and
`STATE_BUCKET` must be present — see below for what happens if it is not.

### State lives in the bucket, and must

Cloud Run's filesystem is ephemeral. `build_store()` selects `GcsStateStore` when
`STATE_BUCKET` is set and **silently falls back to a local file when it is not** — which on
Cloud Run means every tick reads no state, decides `FIRST_RUN`, and posts. The symptom is a
channel full of duplicates, one per tick, forever. `setup.ps1` sets the variable twice
(once in `deploy/env.yaml`, once as an explicit override derived from `-Project`) and then
verifies it landed in the deployed spec.

There is no locking or compare-and-swap on the GCS object. That is safe here because
exactly one tick runs at a time: the schedule is two-hourly, a tick takes about three
seconds, and the job is deployed with `--max-retries 0`.

### Why `--max-retries 0`

Unusual for a scheduled job, and deliberate. Walk the failure modes:

- **The Hyperliquid read fails.** Nothing posted, nothing saved. The next tick
  recovers, and no crossing is lost — `decide` compares the *live* band against the
  *stored* one, so a crossing is delayed, never dropped.
- **The Slack post fails.** `run_tick_async` raises before `store.save()`, so state stays
  put and the next tick retries by itself.
- **Slack succeeds, then the state write fails.** A retry re-reads stale state and posts a
  duplicate.

The only case a retry would change is the one where it causes a duplicate alert. Cloud
Scheduler keeps `--max-retry-attempts 3`, but those cover a failed *enqueue* and cannot
double-run a tick that has already started.

### Cadence: `HEARTBEAT_HOURS=0` in the deployed job

`decide` suppresses a tick when `now - last_post_ts < interval`, and `runner` records
`last_post_ts` at tick *start*. A cron set to the same period as the interval puts those
two values almost exactly one interval apart, so a second or two of scheduler jitter
decides whether the delta clears it — and roughly every other run it does not. The bot
would post at twice the intended gap, unpredictably. The old 10-minute poll masked this.

`deploy/env.yaml` therefore sets both interval knobs to `0`, which makes the check always
pass, so cadence is exactly the cron: one post every two hours, deterministically. The
guard is not gone, it has moved to Cloud Scheduler — which, unlike GitHub's queue, is
punctual enough to be the thing that governs the rate.

**If you ever make the cron faster than the intended post rate, put the real number back.**
That is what the guard is for, and the two knobs remain independent: the cron is how often
the bot *looks*, `HEARTBEAT_HOURS` is how often it is *allowed to post*.

Two consequences of leaning on the cron, both accepted when the team asked for a quieter
channel. A band crossing is only seen at the next poll, so an 80% or 90% alert can arrive
up to two hours late. And `ESCALATE_AT` cannot post more often than the cron does, so the
escalation rule is inert; restoring it means giving both knobs real values and living with
the jitter above.

### Operating it

```powershell
gcloud run jobs executions list --job hl-usdc-tick --region asia-southeast1
gcloud run jobs execute hl-usdc-tick --region asia-southeast1 --wait  # force one now
gcloud scheduler jobs pause  hl-usdc-tick-hourly --location asia-southeast1
gcloud scheduler jobs resume hl-usdc-tick-hourly --location asia-southeast1
# ^ named when the trigger was hourly; it now fires every two hours. Renaming it
#   would leave the original job firing as well, which is why it kept the name.
gcloud storage cat gs://<project>-hl-usdc-state/state.json            # what it remembers
.\deploy\set-webhook.ps1 -Project <project>                           # rotate the webhook

# redeploy after a code change
gcloud run jobs deploy hl-usdc-tick --source . --region asia-southeast1
```

To force a fresh heartbeat, delete the state object:
`gcloud storage rm gs://<project>-hl-usdc-state/state.json`. The next tick reports
`FIRST_RUN` and posts.

The webhook is read as `:latest`, so a rotation takes effect on the next tick with no
redeploy.

### Sharing a project with another bot

This project also hosts the perp-premiums digest, so every resource here is named
`hl-usdc-*` and the secret is `hl-usdc-slack-webhook-url` — the other bot owns
`slack-webhook-url`. The two use **different webhooks pointing at different channels**, and
crossing them would be silent: each URL is valid, just wrong. If a deploy ever puts this
bot's messages in the wrong channel, that is the first thing to check.

Cloud Scheduler's free tier covers three jobs per billing account; this is the second.

## Alternative: Oracle Cloud Always Free VM

No port needed: the VM runs the ordinary CPython path (`LocalFileStateStore` plus
`python -m hl_usdc_bot.tick_once`), driven by a systemd timer.

### Read this first: the idle-reclaim risk

Oracle reclaims Always Free compute instances that look idle. An instance is judged idle
when, over a rolling 7-day window, **95th-percentile CPU is under 20% and network is
under 20%** (plus memory under 20% on Ampere A1 shapes).

This bot runs for about three seconds every two hours. It sits near 0% on every one of those
metrics, which makes it a textbook reclamation candidate. Oracle also halved the Always
Free A1 allowance in June 2026 (4 OCPU/24 GB → 2 OCPU/12 GB) with no announcement.

Mitigations, honestly ranked:

1. **Upgrade the tenancy to Pay As You Go.** Always Free resources stay free, and the
   idle-reclaim policy is widely reported not to apply to PAYG accounts. Verify this
   before relying on it, and set a budget alert — a PAYG account *can* incur charges.
2. **Give the box other work** that keeps it genuinely busy.
3. **Accept the risk and watch for it.** Reclamation is not silent data loss: the
   instance stops, Slack goes quiet, and you notice. `state.json` is the only thing on
   the box, and losing it costs one heartbeat.

If this matters more than "free forever", Modal is a ~30-line port away and has no such
policy.

### 1. Create the account

cloud.oracle.com/free. A card is required for identity verification; Always Free
resources are not charged against it.

**Your home region cannot be changed later.** From the Philippines, pick Singapore or
Tokyo — closer, and historically better free-tier capacity than the US regions.

### 2. Create the instance

Compute → Instances → **Create instance**.

- **Shape: `VM.Standard.E2.1.Micro`** (AMD, 1/8 OCPU, 1 GB). Choose this over Ampere A1:
  A1 frequently returns "Out of host capacity", and this workload needs almost nothing.
  1 GB is ample.
- **Image:** Oracle Linux 9 or Ubuntu 24.04 — `bootstrap.sh` handles both.
- **SSH keys:** upload your public key, or let Oracle generate one and save it.

No ingress rules are needed beyond SSH. The bot only makes outbound calls.

### 3. Ship the code

From this repo on your machine (Windows has OpenSSH built in):

```powershell
scp -r -i <your-key> hl_usdc_bot deploy ubuntu@<INSTANCE_IP>:~/hl-usdc-bot/
ssh -i <your-key> ubuntu@<INSTANCE_IP>
```

The user is `ubuntu` on Ubuntu images and `opc` on Oracle Linux.

### 4. Install

```bash
cd ~/hl-usdc-bot
sudo SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...' ./deploy/bootstrap.sh
```

The script installs Python, creates an unprivileged `hlbot` user, builds a virtualenv
with only `requests`, writes `/etc/hl-usdc-bot.env` at mode 0640, installs the systemd
units, enables the timer, and runs one tick so you see the result immediately.

It is idempotent — re-run it to ship changes. Omit `SLACK_WEBHOOK_URL` on a re-run to
keep the installed value.

### 5. Confirm it works

```bash
journalctl -u hl-usdc-bot.service -n 20        # the tick that just ran
systemctl list-timers hl-usdc-bot.timer        # when the next one fires
sudo cat /var/lib/hl-usdc-bot/state.json       # proof state persisted
```

Expect `reason=FIRST_RUN post=True` and a Slack message. Then:

```bash
sudo systemctl start hl-usdc-bot.service
journalctl -u hl-usdc-bot.service -n 5
```

That second run must log `reason=SUPPRESSED post=False` and send nothing.

### Operating it

```bash
journalctl -u hl-usdc-bot.service -f           # follow live
journalctl -u hl-usdc-bot.service --since today
sudo systemctl start hl-usdc-bot.service       # force a tick now
sudo systemctl disable --now hl-usdc-bot.timer # pause alerts
sudo rm /var/lib/hl-usdc-bot/state.json        # force a fresh heartbeat
sudoedit /etc/hl-usdc-bot.env                  # change cadence, then restart the timer
```

`Persistent=true` on the timer means a reboot or a stopped VM catches up on the missed
run rather than skipping it silently. The bot's own cadence logic then decides whether
that catch-up tick warrants a post, so you get at most one message, not a backlog.

Logs go to journald, which rotates them by default — no logrotate config needed.

## Alternative: Cloudflare Workers

Free forever, punctual to the minute, and no credit card. Python Workers are in
**open beta**, so expect the tooling to move.

### 0. Prerequisites

- A free Cloudflare account (no card required)
- [Node.js](https://nodejs.org) and [uv](https://docs.astral.sh/uv/)

```bash
npx wrangler login
```

### 1. Slack webhook

api.slack.com/apps → **Create New App** → **From scratch** → name it and pick your
workspace → set the name and icon under **Basic Information → Display Information**
(the payload cannot override these) → **Incoming Webhooks** → toggle **On** → **Add New
Webhook to Workspace** → choose the channel → copy the
`https://hooks.slack.com/services/...` URL.

### 2. Create the KV namespace

```bash
npx wrangler kv namespace create HL_BOT_STATE
```

Paste the returned `id` into `wrangler.jsonc`, replacing `<PASTE_KV_NAMESPACE_ID>`.

### 3. Store the webhook as a secret

```bash
npx wrangler secret put SLACK_WEBHOOK_URL
```

A **secret**, not a `var`. Values under `vars` in `wrangler.jsonc` are committed to the
repo; secrets are not.

For local development, put it in `.dev.vars` instead (gitignored):

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

### 4. Deploy

```bash
uvx --from workers-py pywrangler deploy
```

`pywrangler` vendors the dependencies listed in `pyproject.toml`. That list is
**intentionally empty** — the Worker path uses only the standard library, which keeps
`requests`, `flask` and `google-cloud-storage` out of the bundle. Those are CPython-only
and live in `requirements.txt`, which Python Workers does not read.

### 5. Confirm it works

Exercise the cron path locally without waiting for the next tick:

```bash
uvx --from workers-py pywrangler dev
# then, in another terminal:
curl "http://localhost:8787/__scheduled"
```

Then watch the deployed Worker live:

```bash
npx wrangler tail
```

Expect a log line like `utilization=0.6449 band=NORMAL reason=FIRST_RUN post=True` and
one Slack message. The next tick two hours later must log `reason=SUPPRESSED post=False`
and send nothing — that second tick is the real test, because it proves state is
round-tripping through KV.

Hitting the Worker's URL in a browser returns a status string and **deliberately does not
run a tick**. An HTTP endpoint that posted to Slack would let anyone who found the URL
spam the channel.

### Operating it

```bash
npx wrangler tail                                    # live logs
npx wrangler kv key get --binding HL_BOT_STATE state.json --remote
npx wrangler kv key delete --binding HL_BOT_STATE state.json --remote  # force a heartbeat
uvx --from workers-py pywrangler deploy              # ship changes
```

Change cadence by editing `vars` in `wrangler.jsonc` and redeploying.

### Free tier headroom

| Limit | Free allowance | This bot uses |
|---|---|---|
| Worker requests | 100,000/day | 24/day |
| KV reads | 100,000/day | 24/day |
| KV writes | 1,000/day | ~5/day |
| Cron triggers | 5 per account | 1 |

### Running elsewhere

The bot is host-agnostic: `build_store()` picks a state backend from configuration and
`run_tick_async` awaits whatever its collaborators return. `LocalFileStateStore`,
`GcsStateStore` (Cloud Run), and `KvStateStore` (Workers) are interchangeable.

`app.py` and `wsgi.py` still hold a working Cloud Run **Service** — an authenticated
`POST /tick` behind `--no-allow-unauthenticated` — and `tests/test_app.py` still covers
it. The deployed job does not use them; it runs `tick_once` as a batch container. Build
the same image and override the entrypoint if you want the Service instead.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(required)* | Incoming webhook. Optional when `DRY_RUN=1`. |
| `DRY_RUN` | `0` | Print the payload instead of sending it. |
| `HEARTBEAT_HOURS` | `1` | Minimum gap between posts while quiet. `0` in the deployed job. |
| `ESCALATED_INTERVAL_HOURS` | `1` | Minimum gap once escalated. `0` in the deployed job. |
| `ESCALATE_AT` | `0.79` | Utilization that switches to the fast cadence. |
| `STATE_FILE` | `state.json` | Local state path (VM and local dev). |
| `STATE_BUCKET` | *(unset)* | GCS bucket. When set, overrides `STATE_FILE`. |
| `STATE_OBJECT` | `state.json` | Key name in GCS or Cloudflare KV. |
| `DISPLAY_TIMEZONE` | `Asia/Manila` | Timestamp rendering. |
| `DISPLAY_UTC_OFFSET_HOURS` | `8` | Fallback when the runtime has no IANA database. |
| `TOKEN_INDEX` | `0` | Hyperliquid token index; `0` is USDC. |

Settings are read from the real environment, falling back to a local `.env` file
(gitignored, and excluded from the Docker image). Real environment variables always win,
so a value injected by systemd, Secret Manager or a Worker binding can never be shadowed
by a stray `.env`. Copy `.env.example` to `.env` to get started.

Timestamps print the IANA zone name rather than an abbreviation, because Manila's
abbreviation is `PST` — which would read as US Pacific. On runtimes without the IANA
database (Cloudflare's Pyodide), `DISPLAY_UTC_OFFSET_HOURS` is used instead; the
Philippines observes no DST, so a fixed +08:00 is exact.

## Supported hosts

| Host | State backend | Entry point | Notes |
|---|---|---|---|
| **Google Cloud Run Job** | `GcsStateStore` | `hl_usdc_bot.tick_once` via Cloud Scheduler | Primary. Punctual to the second; needs billing enabled. |
| Oracle Cloud VM | `LocalFileStateStore` | `hl_usdc_bot.tick_once` via systemd timer | No caps; idle-reclaim risk. |
| Cloudflare Workers | `KvStateStore` | `worker.py` | Free forever; 10 ms CPU/invocation on the free plan. |
| Cloud Run Service | `GcsStateStore` | `wsgi.py` + `app.py` | Same image, different entrypoint. Not deployed. |
| Anywhere else | `LocalFileStateStore` | `hl_usdc_bot.tick_once` | Any scheduler that can run a command. |

GitHub Actions was the original host and was retired because its scheduled runs drifted
by hours; see [Why this replaced GitHub Actions](#why-this-replaced-github-actions).

`build_store()` picks the backend from configuration, and `run_tick_async` awaits
whatever its collaborators return, so sync and async stores are interchangeable.

## Cost

Effectively nothing on Cloud Run. ~730 ticks/month at roughly 3 seconds each is about 2k
vCPU-seconds against Cloud Run's 180k free tier; Cloud Scheduler's first three jobs are
free; the image fits inside Artifact Registry's 0.5 GB free tier. The state object is a
few hundred bytes. Secret Manager is the only line item that is not zero, at about
**$0.06/month**.

Zero on the Oracle Always Free tier, if you would rather not enable billing at all.
