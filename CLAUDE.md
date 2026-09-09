# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All Python invocations go through the venv interpreter (Windows layout):

```powershell
.\.venv\Scripts\python.exe -m pytest                      # full suite
.\.venv\Scripts\python.exe -m pytest tests/test_decide.py  # one file
.\.venv\Scripts\python.exe -m pytest tests/test_bands.py::test_hysteresis_holds_high_within_the_buffer
```

`pyproject.toml` sets `pythonpath = ["."]`, so tests import `hl_usdc_bot` without an install step.

Run a real tick against the live market without sending to Slack:

```powershell
$env:DRY_RUN="1"; $env:STATE_FILE=".\state.json"
.\.venv\Scripts\python.exe -m hl_usdc_bot.tick_once
```

Delete `state.json` to reset cadence. Run twice to confirm suppression works.

`Config.from_env()` reads a gitignored `.env` when called with no argument; real
environment variables take precedence over it. Passing an explicit mapping
(`Config.from_env({...})`) skips `.env` entirely — that is what keeps a developer's real
webhook out of test runs, so keep writing tests that way.

Deployment is fully scripted in `deploy/setup.ps1` and documented in README.md — do not
re-derive those commands. The primary target is a **Cloud Run Job** fired hourly by Cloud
Scheduler, in project `plucky-vision-508102-k7` / `asia-southeast1`, which it shares with
the sibling perp-premiums bot. Cloudflare Workers, an Oracle VM and a Cloud Run Service
are documented alternatives.

GitHub Actions was the original host and was retired in favour of Cloud Run because its
scheduled runs drifted by hours against a one-hour target. `setup.ps1` is idempotent — to
ship a code change, re-run it, or `gcloud run jobs deploy hl-usdc-tick --source . --region
asia-southeast1`.

`worker.py` cannot be imported by CPython (`workers`, `js`, `pyodide` exist only inside
the Workers runtime), so it is not covered by the suite. Keep it thin: anything with
logic in it belongs in `hl_usdc_bot`, where it can be tested. `py_compile` is the only
local check available for it.

## Architecture

A scheduler fires hourly → `runner.run_tick_async` reads Hyperliquid, loads prior state,
calls `decide`, posts to Slack if warranted, then saves state. The scheduler is a systemd
timer on the VM, a Cron Trigger on Cloudflare, or Cloud Scheduler on Cloud Run; the
orchestration is identical in all three.

`run_tick_async` is the single orchestration, shared by every host. It awaits whatever
its collaborators return (`_resolve`), so async collaborators (Cloudflare KV, the Workers
`fetch`) and sync ones (`LocalFileStateStore`, `requests`) are interchangeable. The sync
`run_tick` is just `asyncio.run` around it. **Do not fork this function per host** — the
ordering guarantee below is exactly what would drift.

**`decide.py` is a pure function and must stay that way.** It takes
`(reading, previous_state, now, config)` and returns a `Decision`. No clock, no network,
no disk. All behaviour changes belong here, driven by tests that pass synthetic readings
directly rather than waiting on real market moves. Everything else in the package is thin
glue around it — `app.py` in particular should contain almost no logic.

### Three orthogonal rules — do not conflate them

- **Polling** (the Cloud Scheduler cron, hourly at `:00`) controls *how often we look*. It
  cannot raise Slack volume by itself: `decide` suppresses anything not due. The Actions
  host polled every 10 minutes to get six chances at beating GitHub's queue; Cloud
  Scheduler is punctual, so one poll an hour does what six could not.
- **Cadence** (`heartbeat_hours`, `escalated_interval_hours`, and `escalate_at` at 0.79)
  controls *how often* to post: the minimum gap between messages, and the shorter gap
  that applies once utilization reaches 79%. **Both are `0` in the deployed job** — see
  the cadence invariant below before changing either the cron or these.
- **Severity band** (`bands.py`) controls *how loud* the post is. A band change posts
  immediately regardless of cadence.

A reading can escalate cadence while still sitting in the `NORMAL` band — 79% is
deliberately below the 80% band boundary. That is the design, not a bug.

`test_polling_faster_than_the_heartbeat_does_not_multiply_posts` in `tests/test_decide.py`
pins the polling property directly, so a faster cron cannot silently become a louder
channel.

### Hysteresis

Bands have a 1.5-point exit buffer (`bands.EXIT_BUFFER`): entering `HIGH` needs ≥ 80%, but
leaving it needs < 78.5%. Without this, a reading hovering at 80.00% alerts every tick.
The 78.5% exit sitting below the 79% cadence trigger is intentional — cadence must calm
down before the band does, never the reverse.

## This repository is public

- **The Slack webhook is a bearer credential and lives only in Secret Manager**, as
  `hl-usdc-slack-webhook-url`, mounted into the job as `SLACK_WEBHOOK_URL` at `:latest`.
  It must never appear in `deploy/env.yaml`, in a tracked file, in an image layer or in a
  log. `.env` holds it locally and is gitignored and `.dockerignore`d — keep it that way.
  `deploy/set-webhook.ps1` is the only supported way to write or rotate it.
- **This bot's webhook is not the perp-premiums bot's webhook.** They share a GCP project
  and post to different channels. Crossing them fails silently — each URL is valid, just
  wrong — so every resource here is named `hl-usdc-*` to make the mix-up hard.
- **`state.json` stays tracked, but it is now only the local-dev and VM seed.** The
  deployed job reads and writes `gs://<project>-hl-usdc-state/state.json` instead, so the
  tracked copy is frozen at the cutover and will look stale. That is expected. It holds
  only a timestamp, a utilization figure and a band name — nothing sensitive. It used to
  be committed every run to stop GitHub disabling the cron for inactivity; with the
  workflow gone, that reason is gone too.

## Invariants that have already caused bugs

- **`Band` is an `IntEnum` and `Band.NORMAL == 0`, so it is falsy.** Always use
  `is not None`, never truthiness. A truthiness check in `BotState.to_dict` silently
  serialised `NORMAL` as `null`, which made the bot forget its band and re-fire
  "Crossed 80%" alerts.
- **Every numeric field from the Hyperliquid API arrives as a string.** Parse to `Decimal`
  and never route through `float` — `hyperliquid.parse_reserve_state` is the only place
  that conversion should happen.
- **State advances only after Slack returns 2xx** (`runner.run_tick`). A Slack failure must
  leave the previous band intact so the next tick retries instead of swallowing a crossing.
- **Suppressed ticks must not touch `last_post_ts`.** Restarting the heartbeat clock on
  every quiet tick means the 6-hourly post never comes due.
- **`STATE_BUCKET` must be set in any deployment with an ephemeral filesystem.**
  `build_store()` falls back to `LocalFileStateStore` when it is empty, and the fallback
  is *silent*. On Cloud Run that means every tick reads no state, decides `FIRST_RUN` and
  posts — an hourly stream of duplicates. `deploy/setup.ps1` sets it twice (in
  `deploy/env.yaml` and again as an override derived from `-Project`) and then asserts it
  landed in the deployed spec before it will finish. Do not remove that check.
- **The job is deployed with `--max-retries 0`, on purpose.** A failed Hyperliquid read
  saves nothing and the next tick recovers; a failed Slack post raises before
  `store.save()` so the next tick retries by itself. The only case a retry would change is
  a Slack post that succeeded followed by a state write that did not — and there a retry
  re-reads stale state and posts a duplicate. Retrying can only hurt. (Cloud Scheduler's
  own `--max-retry-attempts 3` is different: it retries a failed *enqueue* and cannot
  double-run a tick already in flight.)
- **`HEARTBEAT_HOURS` and `ESCALATED_INTERVAL_HOURS` are `0` in the deployed job, and the
  cron is what governs the post rate.** `decide` suppresses when
  `now - last_post_ts < interval` and `runner` stamps `last_post_ts` at tick *start*, so
  an hourly cron against a 1-hour interval lands within seconds of the boundary and a
  little scheduler jitter decides it — roughly every other hour would suppress, and the
  bot would post every two hours. Zero makes the check always pass. **If the cron is ever
  made faster than the intended post rate, put a real interval back in the same change**,
  or every poll becomes a message.
- **`GcsStateStore.load` catches only `NotFound`.** Other GCS errors propagate on purpose:
  treating a transient failure as "first run" would post a spurious heartbeat and discard
  the tracked band.
- **Never call `create_app()` at import time.** `wsgi.py` exists so importing
  `hl_usdc_bot.app` never reads the environment as a side effect.
- **Nothing on the Worker code path may import `requests`, `flask`, `google-cloud-storage`
  or `tzdata` at module scope.** Pyodide has none of them. `GcsStateStore` imports google
  inside `__init__`, `post_webhook` imports `requests` inside the function, and
  `resolve_display_tz` falls back to a fixed UTC offset when the IANA database is absent.
  `tests/test_portability.py` enforces this by importing the modules with those packages
  blocked — if it fails, the Worker deploy would fail too.
- **`pyproject.toml` `dependencies` must stay empty.** `pywrangler` vendors that list into
  the Worker bundle; CPython-only dependencies belong in `requirements.txt`, which Python
  Workers does not read.
- **The Worker's `fetch` handler must never run a tick.** It is publicly reachable; a tick
  there would let anyone who found the URL spam the Slack channel. Cron is the only
  trigger.

## Conventions

- Timestamps render the IANA zone name (`Asia/Manila`), not the abbreviation — Manila's
  abbreviation is `PST`, which reads as US Pacific. `zoneinfo` needs the `tzdata` package
  on Windows, and is absent entirely on Pyodide; `DISPLAY_UTC_OFFSET_HOURS` is the
  fallback. The Philippines observes no DST, so a fixed +08:00 is exact there.
- `tests/test_decide.py` exports `CONFIG`, `NOW`, `reading()` and `posted()`; other test
  modules import them rather than redefining fixtures.
- `"pp"` is a substring of `"Supplied"`, so negative assertions about the delta field
  check for `"Δ"` instead.
- The Dockerfile pins `python:3.14-slim` to match the local interpreter, and its
  `ENTRYPOINT` is `python -m hl_usdc_bot.tick_once` — a batch process that exits, which is
  what Cloud Run Jobs expects. `wsgi.py` is still copied in so the same image can serve
  the Cloud Run Service path under a `--command` override, but nothing deployed uses it.
- The VM installs only `deploy/requirements-vm.txt` (`requests`). Flask, gunicorn and the
  GCS client are not needed there and are deliberately absent.
- The systemd timer sets `Persistent=true`, so a reboot replays the missed tick. That is
  safe precisely because `decide` is time-based rather than run-count-based: a catch-up
  tick produces at most one message, not a backlog. The same property is why a dropped
  Cloud Run execution costs one reading and never a stale thread.
- `Config` defaults are `heartbeat_hours = 1` and `escalated_interval_hours = 1`, which
  leaves the `ESCALATE_AT` escalation rule inert — it only does work when the two differ.
  Those defaults are for local runs; the deployed job overrides both to `0`. Leave the
  defaults alone: `tests/test_config.py` pins them, and the Cloudflare and VM hosts use
  their own values.
