# buzerkostka-claude-indicator

Turn a [Pajeníčko **BUZERKOSTKA**](https://pajenicko.cz/zaciname-buzerkostka)
— an ESP32 with a single WS2812 RGB LED, controlled over MQTT — into a
physical status light for [Claude Code](https://claude.com/claude-code).

Glance at the cube instead of the terminal:

| State | Light | Meaning |
| --- | --- | --- |
| `idle` | green, slowly breathing | waiting for your prompt |
| `working` | solid red | Claude is doing something (including compaction) |
| `permission` | orange, blinking 2 Hz | **waiting for your answer** — a permission prompt, a question, an idle nudge |
| *(no sessions)* | dark | nothing running |

Three colours, on purpose: it is the same language as the
[macOS notch indicator](../macos-claude-indicator), so the cube and the
screen always agree. Everything above is configurable — colours, effects,
timings, and which Claude Code event maps to which state — and richer
palettes (a green flash on finish, magenta on error) are a few lines of
JSON away, see [Customising](#customising).

> 🇨🇿 **[Česká verze návodu →](README.cs.md)**

---

## Requirements

- A BUZERKOSTKA already on your network and reachable over MQTT (or over
  plain HTTP on the LAN)
- Claude Code
- `python3` ≥ 3.8 — **that's it**

There are no dependencies. The MQTT client is implemented against the
stdlib, so there is no `pip install`, no virtualenv and no lockfile to
rot. It runs on the `python3` that already ships with macOS.

## Install

```bash
git clone https://github.com/fejtcz/buzerkostka-claude-indicator
cd buzerkostka-claude-indicator
./install.sh
```

`install.sh` links `buzerkostka` into `~/.local/bin`, walks you through
the configuration, offers a hardware test, and registers the Claude Code
hooks. Start a new Claude Code session and the cube comes alive.

<details>
<summary>Install as a Claude Code plugin instead</summary>

```
/plugin marketplace add fejtcz/buzerkostka-claude-indicator
/plugin install buzerkostka-indicator@mafy-hardware
```

The plugin bundles the hooks, so you skip `buzerkostka install` — but you
still need a config file for the broker details:

```bash
~/.claude/plugins/*/buzerkostka-indicator/bin/buzerkostka setup
```
</details>

<details>
<summary>Install with pipx / pip</summary>

```bash
pipx install git+https://github.com/fejtcz/buzerkostka-claude-indicator
buzerkostka setup
buzerkostka install
```
</details>

## Configure

`buzerkostka setup` writes `~/.config/buzerkostka/config.json`. The only
two things it cannot guess are your broker and your cube's topic:

```json
{
  "transport": "mqtt",
  "mqtt": {
    "host": "192.168.1.10",
    "port": 1883,
    "username": "claude",
    "password": "…"
  },
  "device": {
    "topic": "devices/kostka/A0B1C2D3E4F5",
    "channel_order": "rgb"
  }
}
```

Don't know the topic? Either read it from the cube's web portal, or:

```bash
buzerkostka discover      # then power-cycle the cube
```

The cube announces itself to `devices/register/kostka` on every connect,
and `discover` will save the topic for you.

### Other ways to reach the cube

| `transport` | Use when |
| --- | --- |
| `mqtt` | Your own broker (Mosquitto, Home Assistant). Best latency, recommended. |
| `mqtt` → `buzer.cz` | The public broker from the firmware defaults. Works out of the box; animation frames travel over the internet. |
| `http` | No broker at all — `GET /set-color` straight at the cube's IP. Needs a stable IP; drop `render.fps` to ~8. |
| `console` | No hardware. Renders ANSI swatches in your terminal — handy for trying out palettes. |

### Everything works without a config file too

```bash
export BUZERKOSTKA_MQTT_HOST=192.168.1.10
export BUZERKOSTKA_TOPIC=devices/kostka/A0B1C2D3E4F5
```

These are the settings that have an environment override; everything else
lives in the config file, of which
[`config.example.json`](config.example.json) is a fully populated copy.

| Variable | Config key |
| --- | --- |
| `BUZERKOSTKA_TRANSPORT` | `transport` |
| `BUZERKOSTKA_MQTT_HOST` | `mqtt.host` |
| `BUZERKOSTKA_MQTT_PORT` | `mqtt.port` |
| `BUZERKOSTKA_MQTT_USERNAME` | `mqtt.username` |
| `BUZERKOSTKA_MQTT_PASSWORD` | `mqtt.password` |
| `BUZERKOSTKA_MQTT_TLS` | `mqtt.tls` |
| `BUZERKOSTKA_HTTP_HOST` | `http.host` |
| `BUZERKOSTKA_HTTP_PORT` | `http.port` |
| `BUZERKOSTKA_TOPIC` | `device.topic` |
| `BUZERKOSTKA_DEVICE_PASSWORD` | `device.password` |
| `BUZERKOSTKA_CHANNEL_ORDER` | `device.channel_order` |
| `BUZERKOSTKA_BRIGHTNESS` | `render.brightness` |
| `BUZERKOSTKA_FPS` | `render.fps` |
| `BUZERKOSTKA_ENABLED` | `enabled` |

Four more variables sit outside the config file itself:
`BUZERKOSTKA_CONFIG` (where the config lives), `BUZERKOSTKA_STATE_DIR`
(where the socket, lock and log live), `BUZERKOSTKA_DISABLE` (ignore every
hook event) and `BUZERKOSTKA_DEBUG` (make the hook client explain itself
on stderr).

## Verify

```bash
buzerkostka doctor    # config, connectivity, daemon, hooks — all in one
buzerkostka test      # red / green / blue / white / off on the cube
buzerkostka demo      # play every state so you can see the vocabulary
buzerkostka status    # what is the light showing right now, and why
```

**If `test` shows the wrong colours**, the stock firmware's channel order
is fighting you. Run `buzerkostka calibrate` — it asks what you actually
see and works out the mapping. The full story is in
[`docs/color-order.md`](docs/color-order.md).

## How it works

The cube's firmware has no effects. It understands exactly one thing:
*"be this RGB colour, now."* Anything that breathes or blinks has to be
rendered frame by frame on the host — which a Claude Code hook, a process
that lives for milliseconds, cannot do.

So the work is split:

```
  Claude Code hook  ──►  bin/buzerkostka-event  ──►  unix socket
   (async, ~25 ms)        (reads stdin, exits)            │
                                                          ▼
                                              buzerkostka daemon
                                       ┌──────────────────────────────┐
                                       │ per-session state machine    │
                                       │ priority aggregation         │
                                       │ 20 fps animation renderer    │
                                       │ one persistent MQTT socket   │
                                       └──────────────┬───────────────┘
                                                      │  {"r":255,"g":0,"b":0}
                                                      ▼
                                                 BUZERKOSTKA
```

Design decisions worth knowing:

- **The daemon starts itself.** The first hook event spawns it, with a
  cooldown so a broken config can't cause a fork storm. You never run
  `buzerkostka daemon` by hand.
- **Hooks are registered `async`.** They cannot influence a permission
  decision or slow a turn down, which makes the integration provably
  observation-only. They also exit `0` unconditionally — an unplugged cube
  must never break your session.
- **Many sessions, one cube.** Every tab gets its own state record and the
  highest-priority one wins, so a tab waiting for confirmation beats three
  tabs quietly working. `buzerkostka status` shows the full picture.
- **Static colours cost nothing.** The render loop sleeps until the frame
  actually changes: solid red publishes once, not 20 times a second. Idle
  CPU is 0%.
- **Nothing gets stuck.** Every state has a timeout, so a Claude Code
  process killed with `SIGKILL` — which never fires `SessionEnd` — decays
  back to idle instead of leaving the cube red forever.
- **Saying "No" is noticed too.** Declining a permission prompt, or
  pressing Esc mid-turn, fires no Claude Code hook at all — not
  `PostToolUse`, not `Stop`, not even `PermissionDenied`. The daemon tails
  each session's transcript for the interruption marker instead, so the
  cube drops back to idle within a second. Turn it off with
  `"daemon": {"watch_transcript": false}`.

## Which events are watched

`buzerkostka install --tier` picks how much detail you want:

| Tier | Hooks | Notes |
| --- | --- | --- |
| `minimal` | 6 | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Notification:permission_prompt`, `Stop`, `SessionEnd` |
| `standard` *(default)* | 14 | adds `PermissionRequest`, `PermissionDenied`, `PostToolUseFailure`, `StopFailure`, idle nudges, elicitation dialogs and compaction |
| `verbose` | 18 | adds `PreToolUse`, subagent start/stop, teammate idle |

`PostToolUse` is in every tier on purpose: it is the first event after you
approve a permission prompt, so without it the cube would keep blinking
orange for the rest of the turn.

`PermissionDenied` is *not* the hook for "the user pressed No" — Claude
Code fires it only when auto-mode's classifier declines a call. A human
declining produces no hook, which is why the transcript watcher exists.

Scope works like any other Claude Code setting:

```bash
buzerkostka install --scope user      # ~/.claude/settings.json (default)
buzerkostka install --scope project   # .claude/settings.json, committed
buzerkostka install --scope local     # .claude/settings.local.json, gitignored
buzerkostka install --print-only      # just show the JSON
```

Installing is idempotent — it replaces its own hooks and never touches
anyone else's — and always leaves a timestamped backup of `settings.json`.

## Customising

Edit `~/.config/buzerkostka/config.json`, then `buzerkostka reload`.

**Calmer light for a shared office:**

```json
{ "render": { "brightness": 0.35 } }
```

**Make "working" pulse so you can tell it apart from a frozen session:**

```json
{ "states": { "working": {
    "effect": "breathe", "period": 2.5, "min": 0.35, "max": 1.0
} } }
```

**Only care about permission prompts?** Map everything else to `off`:

```json
{ "events": {
    "session-start": "off",
    "prompt-submit": "off",
    "stop": "off",
    "permission": "permission"
} }
```

**Want more colours?** Add states and point events at them. This gives a
single green flash when a turn finishes and four magenta blinks on failure,
each returning to whatever the session was doing underneath:

```json
{ "states": {
    "done":  { "color": "#00FF40", "effect": "flash", "count": 1, "period": 0.6, "duty": 0.7, "priority": 50 },
    "error": { "color": "#FF00FF", "effect": "flash", "count": 4, "period": 0.25, "duty": 0.5, "priority": 90 }
  },
  "events": {
    "stop":       { "state": "done",  "base": "idle" },
    "stop-error": { "state": "error", "base": "idle" },
    "tool-error": { "state": "error" }
} }
```

**Effects available:** `solid`, `off`, `breathe`, `blink`, `flash`
(transient — plays `count` times, then reveals the state underneath),
`rainbow`.

**Priorities** decide which session wins when several disagree. Higher is
more urgent; the shipped order is `off 0 < idle 10 < working 30 < permission 80`,
leaving room for your own states in between.

## Commands

```
buzerkostka setup            interactive first-run configuration
buzerkostka install          register the Claude Code hooks
buzerkostka uninstall        remove them again
buzerkostka doctor           diagnose the whole setup
buzerkostka status [--json]  what the light is doing, and why
buzerkostka test             send red/green/blue/white/off
buzerkostka calibrate        detect the cube's channel order
buzerkostka demo [states…]   play every state
buzerkostka discover         find the cube's topic on the broker
buzerkostka color '#RRGGBB'  show one colour immediately
buzerkostka state working    force a state, for testing
buzerkostka event stop       feed one hook event in by hand
buzerkostka pause | resume   mute the light without stopping the daemon
buzerkostka off              clear all sessions and go dark
buzerkostka reload           re-read config.json in the running daemon
buzerkostka stop | restart   daemon lifecycle
buzerkostka logs [-f]        daemon log
buzerkostka config --path    where the config file lives
```

## Troubleshooting

**Nothing happens at all.** Run `buzerkostka doctor`. It checks the
config, the broker, the daemon and whether the hooks are registered.

**Colours are wrong.** `buzerkostka calibrate`, then see
[`docs/color-order.md`](docs/color-order.md).

**The light is stuck.** `buzerkostka status` shows every session and how
long it has been in its current state. `buzerkostka off` clears them all.

**Hooks don't seem to fire.** They are picked up when a session starts —
open a new one, or run `/hooks` to check. `BUZERKOSTKA_DEBUG=1` makes the
hook client explain itself on stderr, and `buzerkostka logs -f` follows
the daemon.

**Your Claude Code build rejects `"async": true`.** Reinstall with
`buzerkostka install --no-async`. Hooks then run synchronously, costing
roughly 25 ms each.

**`doctor` says your state directory is not writable.** On some machines
`~/.local/state` ends up owned by root — `sudo gem install` and a few
other tools create it that way. The daemon falls back to a private
directory under `$TMPDIR` and keeps working, so this is a warning rather
than a failure. To put it back where it belongs:

```bash
sudo chown -R "$(id -u)":"$(id -g)" ~/.local/state
```

**Turn it off temporarily.** `buzerkostka pause`, or set
`BUZERKOSTKA_DISABLE=1` in the environment Claude Code runs in.

**Driving the cube from a remote dev box.** Point `mqtt.host` at a broker
both machines can reach; the cube doesn't care where the frames come from.
Several machines can share one cube, though they won't know about each
other's sessions.

## Development

```bash
pip install pytest
pytest                       # 129 tests, ~15 s, no hardware needed
BUZERKOSTKA_CONFIG=/tmp/c.json buzerkostka --config /tmp/c.json demo
```

The test suite covers the MQTT wire format against a fake broker, the
effect renderer, session aggregation, `settings.json` merging, and a full
end-to-end run with a real daemon process and real hook invocations. Set
`"transport": "console"` to develop against your terminal instead of the
cube.

## Credits

Hardware and firmware: [Pajeníčko](https://pajenicko.cz) —
[BuzerKostka](https://github.com/Pajenicko/BuzerKostka).
This project is an independent integration and is not affiliated with
Pajeníčko or Anthropic.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
