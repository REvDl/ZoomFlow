# Zoom auto-join bot (Playwright + Docker)

**Status as of 2026-09-15:** core join/mute/leave logic is implemented and has
worked in isolated test runs, but a full end-to-end cycle (scheduled join →
mute → wait → leave) has NOT yet been verified working reliably back-to-back.
See "Known issues" below before relying on this in production.

## Quick start

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_ID in .env
nano config.json   # your schedule

docker compose build
docker compose up -d
docker compose logs -f
```

Getting `ALLOWED_CHAT_ID`: message the bot anything, then check
`https://api.telegram.org/bot<TOKEN>/getUpdates` -> `message.chat.id`.

## Telegram commands

- `/status` — today's schedule and core status (alive/unresponsive).
- `/stop` — immediately leave the current meeting.
- `/disable N` — disable pair with index N (see `/status`) for today.

## Architecture
```
core        — Playwright/Chromium, joins Zoom on schedule from config.json
controller  — Telegram bot + watchdog (docker.sock + Redis heartbeat)
redis       — state, command queue, alert pub/sub (appendonly yes)
```

core and controller talk **only through Redis** (see key schema in
`core/scheduler.py`) — controller never talks to core directly, core never
knows the Telegram token. Alerts that core itself needs to send (e.g. "rejoining
after restart") are published to the `core:alerts` pub/sub channel, and
controller forwards them to the chat.

**Known reliability gap:** `core:alerts` is plain Redis pub/sub — if
`controller` is down/restarting at the moment `core` publishes, that message
is lost silently (no queue/replay). Observed in practice: a "meeting ended"
alert arrived while the corresponding "joining meeting" alert never did,
because controller had restarted in between. If this matters for your use
case, switch `core:alerts` to a Redis list (`RPUSH`/`BLPOP` or `LPOP` polling)
instead of pub/sub before relying on alerts.

## Known issues (as of last testing session)

- **Zoom bot detection.** The Zoom Web Client can show "Automated bots
  aren't allowed to join this meeting" instead of the normal join screen.
  This appeared intermittently during testing — not on every run, with no
  fully understood trigger — and is Zoom's own anti-automation measure, not
  a bug in this code. There is no fix for this in the current architecture;
  if it becomes a persistent blocker, the realistic long-term option is
  migrating from browser automation to the official Zoom Meeting SDK, which
  is a different integration model (no browser page to script) and would
  require rewriting `zoom_automation.py` from scratch.
- **`page.goto(..., wait_until="networkidle")` in `_join_flow()` can time
  out** on the Zoom Web Client because the page has near-constant background
  network activity (polling, chunked resource loads) and may never reach a
  500ms idle window. Fix identified but not yet fully verified across
  multiple runs: switch to `wait_until="domcontentloaded"` and rely on the
  existing `wait_for_selector(NAME_INPUT, ...)` call right after as the real
  readiness check.
- **Leave button click did not reliably trigger the confirm dialog.**
  Debugged extensively (native `dialog` event handler added, console/pageerror
  logging added, accessibility-tree snapshot taken) — root cause was never
  conclusively identified; the confirm dialog is not a native browser dialog,
  and no new DOM/accessibility element appeared after a normal Playwright
  `.click()`, only keyboard focus moved to the Leave button. A
  human-like mouse move + down + pause + up sequence
  (`_click_leave_humanlike`) was written as an attempt at a fix, plus a
  fallback that force-closes the page if no confirmation is detected within
  ~1.5s (relying on Zoom eventually noticing the dropped connection
  server-side). **Not yet verified working over a real end-to-end run.**
- **Microphone unmute delay on join.** Currently the bot joins, then waits
  for the in-meeting footer, then mutes — a multi-second window where the
  mic is live. Not yet fixed; the intended approach is to mute during the
  pre-join lobby screen (before entering audio) rather than after joining,
  but the pre-join screen's DOM/selectors have not yet been captured or
  scripted.

## Important caveats

1. **Selector fragility.** `core/zoom_automation.py` clicks through the Zoom
   Web Client's DOM. Zoom periodically changes its layout — if the bot stops
   finding the "Join from your browser" button or the name field, check the
   selector constants at the top of this file (they're commented with what
   each one targets).
2. **Resources.** On a 1 CPU / 2 GB VPS with no swap, headless Chromium is
   the heaviest process on the box. If you see frequent OOM alerts, either
   raise `mem_limit` for `core` or add a swap file on the host.
3. **Your institution's / meeting organizer's policies.** Automating meeting
   attendance may violate school/work internal rules or Zoom's own terms of
   use — check this yourself before relying on this long-term.
4. **`restart: no` for core** — deliberate choice so a bad config doesn't
   cause an infinite crashloop. After fixing an issue, restart manually:
   `docker compose up -d core`.

## Structure

```
├── docker-compose.yml
├── config.json
├── .env.example
├── core/
│   ├── Dockerfile
│   ├── main.py              # entrypoint, on_startup, sys.exit(1) on bad config
│   ├── config_validator.py  # blocking config.json validation
│   ├── scheduler.py         # state machine + all Redis interaction
│   └── zoom_automation.py   # Playwright: join / waiting room / leave
└── controller/
    ├── Dockerfile
    ├── main.py       # starts bot + both watchdogs + pubsub forwarder
    ├── bot.py         # /status /stop /disable
    └── watchdog.py    # docker.sock events + heartbeat staleness
```

## Next steps (pick up here)

1. Apply the `domcontentloaded` fix in `_join_flow()`, test a join on a short
   (2-3 min) test window in `config.json`.
2. If join succeeds, verify `_click_leave_humanlike()` actually triggers the
   confirm dialog (or falls back to `page.close()` cleanly).
3. If Zoom's bot-detection screen reappears, don't keep retrying immediately
   — it appeared to be intermittent/session-based rather than a hard
   permanent ban in testing, so spacing out attempts is a cheaper first
   thing to try than changing code.
4. Only if the above stays permanently blocked: evaluate Zoom Meeting SDK as
   a full rewrite of the join/leave layer.