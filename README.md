# Zoom auto-join bot (Playwright + Docker)

**Status as of 2026-09-15:** join/mute/wait/leave cycle has completed
successfully end-to-end in **4 real runs** so far — 1 standalone run, plus
3 back-to-back runs in a single session (config below). All 4 finished
cleanly (join → in-meeting → mute → wait → leave-with-confirmation →
cleanup). That's still a small sample — see "Verified vs. still open"
before assuming everything below is airtight.

Test schedule used for the 3-back-to-back run:

```json
[
  {"url": "...", "name": "Иван Иванов", "start": "23:03", "end": "23:05", "days": [1,2,3,4,5]},
  {"url": "...", "name": "Иван Иванов", "start": "23:07", "end": "23:09", "days": [1,2,3,5]},
  {"url": "...", "name": "Иван Иванов", "start": "23:11", "end": "23:13", "days": [2]}
]
```

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
knows the Telegram token. Alerts core itself needs to send (e.g. "rejoining
after restart") go through the `core:alerts` pub/sub channel, forwarded by
controller.

**Known reliability gap (unchanged):** `RedisState.publish_alert` still uses
plain Redis `PUBLISH`. If `controller` is down/restarting at the moment
`core` publishes, that alert is lost with no replay. Not touched in this
version — still needs a switch to a list (`RPUSH`/`BLPOP` or `LPOP`
polling) if guaranteed delivery matters.

## Verified vs. still open

**Verified across the 4 logged runs (2026-09-15):**
- `page.goto(..., wait_until="domcontentloaded")` — no timeout waiting on
  `NAME_INPUT` in any of the 4 runs. The earlier `networkidle` timeout issue
  did not reproduce.
- Leave flow completed with an actual confirmation-button click
  (`Нажата кнопка подтверждения выхода в модальном окне
  (button.leave-meeting-options__btn)`) logged in **all 4/4** runs, followed
  by clean Playwright cleanup. No case yet where the confirm button wasn't
  found.
- Zoom's "Automated bots aren't allowed to join this meeting" screen did
  **not** appear in any of the 4 runs.

**Still open / not resolved by these runs:**
- No automatic fallback exists in `_leave()` if the confirm-button selectors
  ever fail to match — code-level fact, simply hasn't been exercised by
  testing yet since the confirm button was found every time.
- Zoom bot-detection not appearing in 4 runs is a good sign, not proof it's
  gone — no explicit detection/handling for that screen exists in
  `zoom_automation.py` either way.
- Mic-mute-after-join window (see below) is still architecturally the same
  as before — mute happens after joining, not in the pre-join lobby.

## Mic-on-join behavior is inconsistent between runs

Across the 4 runs, the mic was sometimes briefly live (with audible
beeping, since `--use-fake-device-for-media-stream` has no real input) for
a couple seconds before the explicit mute click, and sometimes already
muted on arrival with no beeping and no explicit mute click needed. This
seemed to line up with whether the "Join Audio by Computer" dialog
appeared or not, but with only 3 data points that's not confirmed — worth
watching on future runs rather than treating as understood. Either way,
the pre-join-lobby mute (muting before entering audio at all, so there's no
live window regardless) is still the real fix and isn't implemented yet.

## Scheduling note: the bot leaves a few minutes after the pair's `end`, not right on time

Confirmed by the 3-pair test: the bot actually left each meeting about
**3 minutes** after the configured `end`, not immediately. Since a new pair
can't start until the bot has fully left the previous one, back-to-back
pairs scheduled with only a 2-minute gap ended up starting about a minute
late each time — the bot didn't error out, it just queued up behind the
previous leave.

**Takeaway:** leave at least ~3-4 minutes of buffer between consecutive
pairs in `config.json`, don't schedule them back-to-back with only a minute
or two of gap.

## Other things observed in the logs (benign so far)

- Repeated `[console:error] requestStorageAccess: Permission denied.` on
  every join — did not affect join success in any run, looks like routine
  Zoom Web Client noise in a third-party-cookie-restricted context.
- One `[pageerror] OperationError` during the leave sequence in run 1 — did
  not prevent the confirm click or cleanup from completing.

## Important caveats

1. **Selector fragility.** `core/zoom_automation.py` clicks through the
   Zoom Web Client's DOM (`NAME_INPUT`, `JOIN_BUTTON`, `IN_MEETING_MARKERS`,
   `WAITING_ROOM_MARKERS`, `MEETING_ENDED_MARKERS`, plus the
   `_handle_audio_dialog` selector list). If the bot stops finding these,
   start there.
2. **Resources.** On a 1 CPU / 2 GB VPS with no swap, headless Chromium is
   the heaviest process on the box. Frequent OOM alerts → raise `mem_limit`
   for `core` or add host swap.
3. **Your institution's / meeting organizer's policies.** Automating
   meeting attendance may violate school/work internal rules or Zoom's own
   terms of use — check this yourself before relying on this long-term.
4. **`restart: no` for core** — deliberate, so a bad config doesn't
   crashloop. After fixing an issue: `docker compose up -d core`.

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
│   └── zoom_automation.py   # Playwright: join / waiting room / audio / leave
└── controller/
    ├── Dockerfile
    ├── main.py       # starts bot + both watchdogs + pubsub forwarder
    ├── bot.py         # /status /stop /disable
    └── watchdog.py    # docker.sock events + heartbeat staleness
```

## Next steps

1. Run more back-to-back sessions (ideally with the recommended ≥3-4 min
   gap) to see if the mic-live-vs-pre-muted pattern from run 1/3 vs run 2
   holds up, and whether it's actually tied to the audio dialog or
   coincidental.
2. Keep watching for the Zoom bot-detection screen over more runs before
   treating the anti-detection tweaks (webdriver spoofing, custom UA,
   disabled automation flags) as sufficient — 4 clean runs is encouraging
   but not conclusive.
3. If tight back-to-back scheduling is actually needed, either shrink the
   `+2` grace window in `scheduler.py`, or make `find_active_pair` /
   `_start_session` tolerant of a pair still finishing its leave.
4. Pre-join-lobby mute (before entering audio at all) is still unimplemented
   — DOM/selectors for that screen haven't been captured.
5. `core:alerts` — still plain pub/sub; move to a list if delivery
   guarantees matter.