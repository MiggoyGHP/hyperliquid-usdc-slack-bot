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

Deployment is fully scripted in README.md — do not re-derive those commands. The primary
target is an Oracle Cloud VM running `deploy/bootstrap.sh` (systemd timer + the ordinary
CPython path); Cloudflare Workers and Cloud Run are documented alternatives.

`deploy/bootstrap.sh` is idempotent and is the only supported way to install or update
the VM. It syncs `hl_usdc_bot/` only — never tests, `.env`, or `state.json`.

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

### Two orthogonal rules — do not conflate them

- **Cadence** (`config.escalate_at`, default 0.79) controls *how often* to post: every 6h
  normally, every 1h once utilization reaches 79%.
- **Severity band** (`bands.py`) controls *how loud* the post is. A band change posts
  immediately regardless of cadence.

A reading can escalate cadence while still sitting in the `NORMAL` band — 79% is
deliberately below the 80% band boundary. That is the design, not a bug.

### Hysteresis

Bands have a 1.5-point exit buffer (`bands.EXIT_BUFFER`): entering `HIGH` needs ≥ 80%, but
leaving it needs < 78.5%. Without this, a reading hovering at 80.00% alerts every tick.
The 78.5% exit sitting below the 79% cadence trigger is intentional — cadence must calm
down before the band does, never the reverse.

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
- The Dockerfile pins `python:3.14-slim` to match the local interpreter.
- The VM installs only `deploy/requirements-vm.txt` (`requests`). Flask, gunicorn and the
  GCS client are not needed there and are deliberately absent.
- The systemd timer sets `Persistent=true`, so a reboot replays the missed tick. That is
  safe precisely because `decide` is time-based rather than run-count-based: a catch-up
  tick produces at most one message, not a backlog.
