# Hyperliquid USDC Utilization → Slack

Posts the utilization rate of Hyperliquid's native USDC borrow/lend market to Slack:
a routine heartbeat while things are calm, escalating to hourly updates and louder
alerts as utilization approaches and crosses the 80% rate kink.

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

Two independent rules. Cloud Scheduler ticks hourly; the bot decides whether that
tick is worth a message.

**Cadence — how often it posts**

| Utilization | Posts every |
|---|---|
| any | 1 hour |

Set `HEARTBEAT_HOURS` higher for a quieter channel; `ESCALATE_AT` then switches to
`ESCALATED_INTERVAL_HOURS` above that utilization. With both set to 1 (the default) the
escalation rule is inert and every tick posts.

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
  hyperliquid.py  read the reserve state; Decimal parsing, strict validation
  rates.py        borrow curve, headroom to the kink, $ and % formatting
  bands.py        severity bands + hysteresis
  decide.py       PURE: (reading, prev state, now, config) -> Decision
  state.py        BotState + LocalFileStateStore / GcsStateStore
  slack.py        Block Kit message + webhook delivery
  runner.py       one tick: read -> decide -> post -> remember
  app.py          Flask: POST /tick, GET /healthz
  tick_once.py    same tick, from the command line
  config.py       environment -> frozen Config
wsgi.py           gunicorn entrypoint
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
should report `SUPPRESSED`, proving the 6-hour rule works. Delete `state.json` to
start over.

## Deploying on GitHub Actions

The repository is the deployment. `.github/workflows/tick.yml` runs one tick an hour and
commits `state.json` back, which is also what keeps the schedule alive — GitHub disables
cron on repositories with no commits for 60 days, and the state commit resets that timer
on every run.

### 1. Slack webhook

api.slack.com/apps → **Create New App** → **From scratch** → name it and pick your
workspace → set the name and icon under **Basic Information → Display Information** (the
payload cannot override these) → **Incoming Webhooks** → toggle **On** → **Add New
Webhook to Workspace** → choose the channel → copy the URL.

### 2. Store it as a repository secret

```bash
gh secret set SLACK_WEBHOOK_URL --repo <owner>/<repo>
```

**The repository is public; the secret is not.** GitHub Secrets are encrypted, hidden
from logs, and unavailable to workflows triggered by forked pull requests. Never put the
webhook in the workflow file, in `.env` (gitignored), or anywhere else in the tree.

### 3. Enable and verify

Actions are enabled by default on new repositories. Trigger a run by hand rather than
waiting for the hour:

```bash
gh workflow run "Hyperliquid USDC utilization"
gh run watch
gh run view --log
```

Expect `reason=FIRST_RUN post=True`, one Slack message, and a `chore: record tick …`
commit. Run it a second time straight away: it must log `reason=SUPPRESSED post=False`,
send nothing, and make no commit.

### What to expect from the schedule

GitHub runs scheduled workflows **on a best-effort basis**. Delays of 15–60 minutes are
normal under load, and individual runs are occasionally dropped. "Hourly" means "about
hourly" — you will see gaps.

That is the trade for zero cost and zero infrastructure. If a post must land at a precise
minute, Cloudflare Workers or a VM timer are the alternatives documented below.

Two consequences worth knowing:

- **A dropped run is harmless.** Cadence is driven by the timestamp in `state.json`, not
  by counting runs, so the next tick simply sees that more than an hour has elapsed and
  posts. You lose a reading, never the thread.
- **A band crossing can arrive late.** If utilization crosses 80% at 14:00 and the run
  slips to 14:45, the alert arrives at 14:45. For a lending pool this is fine; for a
  liquidation alert it would not be.

### Operating it

```bash
gh run list --workflow "Hyperliquid USDC utilization" --limit 10
gh workflow run "Hyperliquid USDC utilization"     # force a tick
gh workflow disable "Hyperliquid USDC utilization" # pause alerts
gh workflow enable  "Hyperliquid USDC utilization"
gh secret set SLACK_WEBHOOK_URL                    # rotate the webhook
```

Change cadence by editing the `env:` block in the workflow and pushing. To force a fresh
heartbeat, delete `state.json` and commit.

If runs stop appearing, check whether GitHub disabled the schedule for inactivity — the
Actions tab says so explicitly, and the workflow's own state commits should prevent it.

## Alternative: Oracle Cloud Always Free VM

No port needed: the VM runs the ordinary CPython path (`LocalFileStateStore` plus
`python -m hl_usdc_bot.tick_once`), driven by a systemd timer.

### Read this first: the idle-reclaim risk

Oracle reclaims Always Free compute instances that look idle. An instance is judged idle
when, over a rolling 7-day window, **95th-percentile CPU is under 20% and network is
under 20%** (plus memory under 20% on Ampere A1 shapes).

This bot runs for about three seconds an hour. It sits near 0% on every one of those
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

Exercise the cron path locally without waiting an hour:

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
one Slack message. The next tick an hour later must log `reason=SUPPRESSED post=False`
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
`GcsStateStore` (Cloud Run), and `KvStateStore` (Workers) are interchangeable, and
`app.py` / `wsgi.py` / `Dockerfile` still hold a working Cloud Run deployment if you ever
want punctual cron with a full CPython runtime.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(required)* | Incoming webhook. Optional when `DRY_RUN=1`. |
| `DRY_RUN` | `0` | Print the payload instead of sending it. |
| `HEARTBEAT_HOURS` | `6` | Quiet-market posting interval. |
| `ESCALATED_INTERVAL_HOURS` | `1` | Posting interval once escalated. |
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
| **GitHub Actions** | `LocalFileStateStore` (committed) | `.github/workflows/tick.yml` | Primary. Free on public repos; cron drifts. |
| Oracle Cloud VM | `LocalFileStateStore` | `hl_usdc_bot.tick_once` via systemd timer | No caps; idle-reclaim risk. |
| Cloudflare Workers | `KvStateStore` | `worker.py` | Free forever; 10 ms CPU/invocation on the free plan. |
| Google Cloud Run | `GcsStateStore` | `wsgi.py` + `Dockerfile` | Punctual; needs billing enabled. |
| Anywhere else | `LocalFileStateStore` | `hl_usdc_bot.tick_once` | Any scheduler that can run a command. |

`build_store()` picks the backend from configuration, and `run_tick_async` awaits
whatever its collaborators return, so sync and async stores are interchangeable.

## Cost

Zero on the Oracle Always Free tier, and the workload is far below every published limit:
~730 ticks/month, roughly 3 seconds each, one outbound API call and at most ~24 Slack
messages a day.

For reference, if this ran on metered infrastructure it would come to roughly
**$0.05–0.15/month** in compute.
