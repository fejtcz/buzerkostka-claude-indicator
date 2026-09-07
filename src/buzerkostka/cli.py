"""Command line interface: ``buzerkostka <command>``."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time

from . import __version__, client, config as config_module, hooks as hooks_module
from .colors import CHANNEL_ORDERS, parse_color, to_hex
from .states import build_states, frame_for_transport, render
from .transport import TransportError, build_transport

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return "\x1b[%sm%s\x1b[0m" % (code, text) if USE_COLOR else text


def ok(text: str) -> str:
    return paint("ok", "32") + "   " + text


def warn(text: str) -> str:
    return paint("warn", "33") + " " + text


def fail(text: str) -> str:
    return paint("fail", "31") + " " + text


def swatch(rgb) -> str:
    if not USE_COLOR:
        return to_hex(rgb)
    return "\x1b[48;2;%d;%d;%dm  \x1b[0m %s" % (rgb[0], rgb[1], rgb[2], to_hex(rgb))


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def event_command() -> str:
    return os.path.join(repo_root(), "bin", "buzerkostka-event")


def load_config(args) -> dict:
    return config_module.load(getattr(args, "config", None) or None)


# ----------------------------------------------------------------------
# daemon-facing commands
# ----------------------------------------------------------------------
def cmd_daemon(args) -> int:
    from .daemon import run

    config = load_config(args)
    # ``restart`` reaches this via cmd_restart, whose subparser has no
    # --foreground flag, so the attribute may be missing entirely.
    if getattr(args, "foreground", False):
        return run(config, foreground=True)

    # Detach: re-exec ourselves in a new session so the daemon outlives
    # the shell that started it.
    argv = [sys.executable, os.path.join(repo_root(), "bin", "buzerkostka")]
    if not os.path.exists(argv[1]):
        argv = [sys.executable, "-m", "buzerkostka"]
    argv += ["daemon", "--foreground"]
    if getattr(args, "config", None):
        argv += ["--config", args.config]

    log_path = os.path.join(config_module.state_dir(), "daemon.log")
    with open(os.devnull, "rb") as devnull:
        subprocess.Popen(
            argv,
            stdin=devnull,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        reply = client.request({"cmd": "ping"}, autostart=False, want_reply=True)
        if reply and reply.get("ok"):
            print(ok("daemon running (pid %s)" % reply.get("pid")))
            return 0
        time.sleep(0.1)
    print(fail("daemon did not come up; see %s" % log_path))
    return 1


def cmd_stop(args) -> int:
    reply = client.request({"cmd": "stop"}, autostart=False, want_reply=True)
    if reply and reply.get("ok"):
        print(ok("daemon stopping"))
        return 0
    print(warn("daemon was not running"))
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(0.4)
    return cmd_daemon(args)


def cmd_status(args) -> int:
    reply = client.request({"cmd": "status"}, autostart=False, want_reply=True)
    if args.json:
        print(json.dumps(reply or {"ok": False, "error": "daemon not running"}, indent=2))
        return 0 if reply else 1
    if not reply:
        print(warn("daemon not running (it starts automatically on the next hook)"))
        return 1

    transport = reply.get("transport") or {}
    health = ok("connected") if transport.get("healthy") else fail("disconnected")
    print("daemon      pid %s, up %ss, %s frames sent"
          % (reply.get("pid"), reply.get("uptime_s"), reply.get("frames_sent")))
    print("transport   %s  %s" % (transport.get("target") or "-", health))
    if transport.get("error"):
        print("            %s" % transport["error"])
    last = reply.get("last_color")
    print("showing     %s  %s"
          % (reply.get("active_state") or "off",
             swatch(last) if last else "-"))
    if reply.get("paused"):
        print("            %s" % warn("paused"))
    if reply.get("override"):
        print("            %s" % warn("manual colour override active"))

    sessions = reply.get("sessions") or []
    if not sessions:
        print("sessions    none")
        return 0
    print("sessions    %d" % len(sessions))
    for session in sessions:
        print("  %-10s %-11s %6.1fs  idle %5.1fs  %s"
              % (session["state"], session.get("label") or "-",
                 session["for_s"], session["idle_s"],
                 (session["session"] or "")[:8]))
    return 0


def cmd_event(args) -> int:
    client.request(
        {"cmd": "event", "event": args.event, "session": args.session or "manual",
         "ts": time.time()}
    )
    return 0


def cmd_state(args) -> int:
    reply = client.request(
        {"cmd": "state", "state": args.state, "session": args.session or "manual"},
        want_reply=True,
    )
    if reply and not reply.get("ok"):
        print(fail(reply.get("error") or "failed"))
        return 1
    return 0


def cmd_color(args) -> int:
    rgb = parse_color(args.color)
    client.request({"cmd": "color", "r": rgb[0], "g": rgb[1], "b": rgb[2],
                    "hold": args.hold})
    return 0


def cmd_off(args) -> int:
    client.request({"cmd": "clear"}, want_reply=True)
    return 0


def cmd_pause(args) -> int:
    client.request({"cmd": "pause"}, want_reply=True)
    print(ok("indicator paused (buzerkostka resume to re-enable)"))
    return 0


def cmd_resume(args) -> int:
    client.request({"cmd": "resume"}, want_reply=True)
    print(ok("indicator resumed"))
    return 0


def cmd_reload(args) -> int:
    reply = client.request({"cmd": "reload"}, autostart=False, want_reply=True)
    if not reply:
        print(warn("daemon not running; nothing to reload"))
        return 0
    if not reply.get("ok"):
        print(fail(reply.get("error") or "reload failed"))
        return 1
    print(ok("configuration reloaded"))
    return 0


def cmd_logs(args) -> int:
    path = os.path.join(config_module.state_dir(), "daemon.log")
    if not os.path.exists(path):
        print(warn("no log yet at %s" % path))
        return 1
    argv = ["tail"] + (["-f"] if args.follow else ["-n", str(args.lines)]) + [path]
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 0


# ----------------------------------------------------------------------
# direct hardware commands (bypass the daemon)
# ----------------------------------------------------------------------
@contextlib.contextmanager
def daemon_paused():
    """Keep a running daemon from fighting us over the LED.

    ``test``, ``demo`` and ``calibrate`` open their own connection and
    drive the cube directly. Without this, the daemon's render loop would
    overwrite every frame within milliseconds and the output would be
    unreadable. A daemon the user had already paused stays paused.
    """
    snapshot = client.request({"cmd": "status"}, autostart=False, want_reply=True)
    should_restore = bool(snapshot and snapshot.get("ok")
                          and not snapshot.get("paused"))
    if should_restore:
        client.request({"cmd": "pause"}, autostart=False, want_reply=True)
        time.sleep(0.1)  # let the render loop notice before we start
    try:
        yield
    finally:
        if should_restore:
            client.request({"cmd": "resume"}, autostart=False, want_reply=True)


def cmd_test(args) -> int:
    """Prove the wiring works without involving hooks or the daemon."""
    config = load_config(args)
    problems = config_module.validate(config)
    if problems:
        for problem in problems:
            print(fail(problem))
        print("\nRun %s to fix this." % paint("buzerkostka setup", "1"))
        return 1

    order = (config.get("device") or {}).get("channel_order", "rgb")
    try:
        transport = build_transport(config)
    except TransportError as exc:
        print(fail(str(exc)))
        return 1

    print("target      %s" % transport.describe())
    print("order       %s\n" % order)
    sequence = [("red", "#FF0000"), ("green", "#00FF00"), ("blue", "#0000FF"),
                ("white", "#FFFFFF"), ("off", "#000000")]
    try:
        with daemon_paused():
            for name, hex_color in sequence:
                rgb = parse_color(hex_color)
                payload = frame_for_transport(rgb, 1.0, order)
                transport.send(*payload)
                print("  %-6s %s  ->  payload r=%d g=%d b=%d"
                      % (name, swatch(rgb), payload[0], payload[1], payload[2]))
                time.sleep(args.delay)
    except TransportError as exc:
        print("\n" + fail("send failed: %s" % exc))
        return 1
    finally:
        transport.close()

    print("\n" + ok("all five colours were delivered"))
    print("If the names above did not match what the cube showed, run %s."
          % paint("buzerkostka calibrate", "1"))
    return 0


def cmd_calibrate(args) -> int:
    """Work out the cube's channel order by asking what you actually see.

    The stock firmware hands FastLED ``CRGB(g, r, b)`` while declaring the
    strip as ``GRB``, so whether red and green come out swapped depends on
    the WS2812 revision soldered to your board. Rather than guess, probe.
    """
    config = load_config(args)
    try:
        transport = build_transport(config)
    except TransportError as exc:
        print(fail(str(exc)))
        return 1

    answers = []
    probes = (("first", (255, 0, 0)), ("second", (0, 255, 0)))
    try:
        with daemon_paused():
            for label, payload in probes:
                transport.send(*payload)
                print("\nThe %s probe is on the cube now (raw payload r=%d g=%d b=%d)."
                      % (label, payload[0], payload[1], payload[2]))
                while True:
                    answer = input(
                        "  Which colour do you see? [r]ed / [g]reen / [b]lue: "
                    )
                    answer = answer.strip().lower()[:1]
                    if answer in ("r", "g", "b") and answer not in answers:
                        answers.append(answer)
                        break
                    if answer in answers:
                        print("  You already used that one; pick a different channel.")
                    else:
                        print("  Please answer r, g or b.")
        order = "".join(answers + [ch for ch in "rgb" if ch not in answers])
    except (KeyboardInterrupt, EOFError):
        print("\naborted")
        return 130
    finally:
        try:
            transport.send(0, 0, 0)
        except TransportError:
            pass
        transport.close()

    if order not in CHANNEL_ORDERS:  # pragma: no cover - defensive
        print(fail("could not derive a valid order from those answers"))
        return 1

    print("\nchannel order: %s" % paint(order, "1"))
    if order == "rgb":
        print(ok("your cube needs no remapping"))
    config.setdefault("device", {})["channel_order"] = order
    path = config_module.save(config, getattr(args, "config", None) or None)
    print(ok("saved to %s" % path))
    cmd_reload(args)
    return 0


def cmd_discover(args) -> int:
    """Listen for the cube's own registration message to learn its topic."""
    from .mqtt import MqttClient, MqttError

    config = load_config(args)
    mqtt_config = config.get("mqtt") or {}
    host = args.host or mqtt_config.get("host")
    if not host:
        print(fail("no broker to listen on; pass --host or run 'buzerkostka setup'"))
        return 1

    print("Listening on %s:%s for %ds." % (host, args.port or mqtt_config.get("port", 1883),
                                           args.timeout))
    print(paint("Power-cycle the cube now", "1")
          + " -- it announces itself to devices/register/kostka on every connect.\n")

    mqtt = MqttClient(
        host=host,
        port=args.port or mqtt_config.get("port", 1883),
        username=args.username or mqtt_config.get("username"),
        password=args.password or mqtt_config.get("password"),
        tls=mqtt_config.get("tls", False),
    )
    found = {}
    try:
        mqtt.connect()
        mqtt.subscribe("devices/register/kostka")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            for _topic, payload in mqtt.read_messages(timeout=1.0):
                try:
                    info = json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                device_id = info.get("device_id") or info.get("mac", "").replace(":", "")
                if not device_id or device_id in found:
                    continue
                found[device_id] = info
                print(ok("found %s  ip=%s  fw=%s  rssi=%s"
                         % (device_id, info.get("ip"), info.get("firmware"),
                            info.get("rssi"))))
                print("     topic: devices/kostka/%s" % device_id)
    except MqttError as exc:
        print(fail(str(exc)))
        return 1
    finally:
        mqtt.disconnect()

    if not found:
        print(warn("nothing announced itself; the topic is also shown in the cube's "
                   "web portal"))
        return 1
    if len(found) == 1 and args.save:
        device_id = next(iter(found))
        config.setdefault("device", {})["topic"] = "devices/kostka/%s" % device_id
        path = config_module.save(config, getattr(args, "config", None) or None)
        print(ok("topic saved to %s" % path))
    return 0


def cmd_demo(args) -> int:
    """Play every configured state so you can see the vocabulary."""
    config = load_config(args)
    states = build_states(config.get("states"))
    render_config = config.get("render") or {}
    order = (config.get("device") or {}).get("channel_order", "rgb")
    brightness = float(render_config.get("brightness", 1.0))
    gamma = float(render_config.get("gamma", 2.0))
    fps = int(render_config.get("fps", 20))

    try:
        transport = build_transport(config)
    except TransportError as exc:
        print(fail(str(exc)))
        return 1

    # Shipped states first, then anything the user added in config.json.
    shipped = ("idle", "working", "permission")
    names = args.states or (
        [name for name in shipped if name in states]
        + sorted(name for name in states if name not in shipped and name != "off")
    )
    print("target %s\n" % transport.describe())
    try:
        with daemon_paused():
            for name in names:
                state = states.get(name)
                if state is None:
                    print(warn("no such state: %s" % name))
                    continue
                duration = state.duration or args.seconds
                print("  %-11s %-8s %s for %.1fs"
                      % (name, state.effect, swatch(state.color), duration))
                started = time.time()
                while time.time() - started < duration:
                    rgb = render(state, time.time() - started, gamma)
                    if rgb is None:
                        break
                    transport.send(*frame_for_transport(rgb, brightness, order))
                    time.sleep(1.0 / max(1, fps))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except TransportError as exc:
        print(fail("send failed: %s" % exc))
        return 1
    finally:
        try:
            transport.send(0, 0, 0)
        except TransportError:
            pass
        transport.close()
    return 0


# ----------------------------------------------------------------------
# setup / install / doctor
# ----------------------------------------------------------------------
def _ask(prompt: str, default=None, secret: bool = False):
    suffix = " [%s]" % ("*" * 6 if secret and default else default) if default else ""
    while True:
        try:
            answer = input("%s%s: " % (prompt, suffix)).strip()
        except (KeyboardInterrupt, EOFError):
            raise SystemExit(130)
        if answer:
            return answer
        if default is not None:
            return default
        print("  (required)")


def _ask_choice(prompt: str, choices, default: str) -> str:
    rendered = "/".join(
        paint(c, "1") if c == default else c for c in choices
    )
    while True:
        answer = _ask("%s (%s)" % (prompt, rendered), default).lower()
        if answer in choices:
            return answer
        print("  choose one of: %s" % ", ".join(choices))


def cmd_setup(args) -> int:
    path = getattr(args, "config", None) or config_module.config_path()
    config = config_module.load(path)
    print(paint("Buzerkostka indicator setup", "1"))
    print("Writing to %s\n" % path)

    transport = _ask_choice("How do you reach the cube?",
                            ["mqtt", "http", "console"],
                            config.get("transport", "mqtt"))
    config["transport"] = transport

    if transport == "mqtt":
        mqtt_config = config.setdefault("mqtt", {})
        mqtt_config["host"] = _ask("Broker host", mqtt_config.get("host") or "buzer.cz")
        mqtt_config["port"] = int(_ask("Broker port", mqtt_config.get("port") or 1883))
        mqtt_config["username"] = _ask("Broker username (blank for none)",
                                       mqtt_config.get("username") or "")
        if mqtt_config["username"]:
            mqtt_config["password"] = _ask("Broker password",
                                           mqtt_config.get("password") or "", secret=True)
        mqtt_config["tls"] = _ask_choice("Use TLS?", ["no", "yes"],
                                         "yes" if mqtt_config.get("tls") else "no") == "yes"
        if mqtt_config["tls"] and mqtt_config["port"] == 1883:
            mqtt_config["port"] = 8883

        device = config.setdefault("device", {})
        current_topic = device.get("topic") or ""
        print("\nThe cube's command topic is shown in its web portal; it looks like")
        print("  devices/kostka/A0B1C2D3E4F5")
        topic = _ask("Command topic (or 'discover')", current_topic or "discover")
        if topic == "discover":
            discover_args = argparse.Namespace(
                config=path, host=mqtt_config["host"], port=mqtt_config["port"],
                username=mqtt_config["username"],
                password=mqtt_config.get("password"), timeout=60, save=True,
            )
            config_module.save(config, path)
            cmd_discover(discover_args)
            config = config_module.load(path)
            device = config.setdefault("device", {})
            topic = device.get("topic") or _ask("Command topic")
        device["topic"] = topic
    elif transport == "http":
        http_config = config.setdefault("http", {})
        http_config["host"] = _ask("Cube IP address or hostname",
                                   http_config.get("host") or "")
        http_config["port"] = int(_ask("Port", http_config.get("port") or 80))
        # HTTP is one request per frame, so ease off the animation rate.
        config.setdefault("render", {})["fps"] = int(
            _ask("Animation frames per second", 8)
        )

    device = config.setdefault("device", {})
    device["password"] = _ask("Device password (blank if you left it empty)",
                              device.get("password") or "", secret=True)

    path = config_module.save(config, path)
    print("\n" + ok("saved %s" % path))

    problems = config_module.validate(config)
    if problems:
        for problem in problems:
            print(fail(problem))
        return 1

    if _ask_choice("\nSend a test sequence to the cube now?", ["yes", "no"], "yes") == "yes":
        cmd_test(argparse.Namespace(config=path, delay=0.6))

    if _ask_choice("\nRegister the Claude Code hooks now?", ["yes", "no"], "yes") == "yes":
        cmd_install(argparse.Namespace(
            config=path, scope="user", tier="standard", no_async=False,
            project_dir=None, dry_run=False, print_only=False,
        ))
    return 0


def cmd_install(args) -> int:
    command = event_command()
    if not os.path.exists(command):
        print(fail("cannot find %s -- run this from the cloned repository" % command))
        return 1
    if not os.access(command, os.X_OK):
        os.chmod(command, 0o755)

    if args.print_only:
        print(json.dumps(
            {"hooks": hooks_module.build_hooks(command, args.tier, not args.no_async)},
            indent=2))
        return 0

    try:
        report = hooks_module.install(
            command, scope=args.scope, tier=args.tier, use_async=not args.no_async,
            project_dir=args.project_dir, dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        print(fail(str(exc)))
        return 1

    verb = "would register" if args.dry_run else "registered"
    print(ok("%s %d hooks (%s tier) in %s"
             % (verb, report["added"], report["tier"], report["path"])))
    if report["replaced"]:
        print("     replaced %d previously installed hooks" % report["replaced"])
    if report["backup"]:
        print("     backup: %s" % report["backup"])
    print("     events: %s" % ", ".join(report["events"]))
    print("\nStart a new Claude Code session (or run /hooks) to pick them up.")
    return 0


def cmd_uninstall(args) -> int:
    total = 0
    scopes = [args.scope] if args.scope else list(hooks_module.SCOPES)
    for scope in scopes:
        try:
            report = hooks_module.uninstall(scope, args.project_dir, args.dry_run)
        except (OSError, ValueError) as exc:
            print(warn("%s: %s" % (scope, exc)))
            continue
        if report["removed"]:
            total += report["removed"]
            print(ok("removed %d hooks from %s" % (report["removed"], report["path"])))
            if report["backup"]:
                print("     backup: %s" % report["backup"])
    if not total:
        print(warn("no hooks of ours were installed"))
    return 0


def cmd_config(args) -> int:
    if args.path:
        print(config_module.config_path())
        return 0
    if args.example:
        print(json.dumps(config_module.DEFAULTS, indent=2, ensure_ascii=False))
        return 0
    if args.edit:
        path = config_module.config_path()
        if not os.path.exists(path):
            config_module.save(config_module.load(path), path)
        return subprocess.call([os.environ.get("EDITOR", "vi"), path])
    config = load_config(args)
    printable = {k: v for k, v in config.items() if not k.startswith("_")}
    if not args.show_secrets:
        for section, key in (("mqtt", "password"), ("device", "password")):
            if printable.get(section, {}).get(key):
                printable[section][key] = "***"
    print(json.dumps(printable, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args) -> int:
    print(paint("buzerkostka-claude-indicator %s" % __version__, "1"))
    problems = 0

    print("\n" + paint("python", "1"))
    print("  %s  %s" % (".".join(str(n) for n in sys.version_info[:3]), sys.executable))
    if sys.version_info < (3, 8):
        print(fail("  python 3.8 or newer is required"))
        problems += 1

    print("\n" + paint("configuration", "1"))
    path = getattr(args, "config", None) or config_module.config_path()
    try:
        config = config_module.load(path)
    except config_module.ConfigError as exc:
        print(fail(str(exc)))
        return 1
    print("  %s%s" % (path, "" if os.path.exists(path) else "  (missing, using defaults)"))
    issues = config_module.validate(config)
    for issue in issues:
        print(fail("  " + issue))
    problems += len(issues)
    if not issues:
        print(ok("  valid"))

    print("\n" + paint("device", "1"))
    if issues:
        print(warn("  skipped -- fix the configuration first"))
    else:
        try:
            transport = build_transport(config)
        except TransportError as exc:
            print(fail("  " + str(exc)))
            problems += 1
        else:
            print("  target: %s" % transport.describe())
            try:
                transport.send(0, 0, 0)
                print(ok("  reachable"))
            except TransportError as exc:
                print(fail("  unreachable: %s" % exc))
                problems += 1
            finally:
                transport.close()

    print("\n" + paint("daemon", "1"))
    reply = client.request({"cmd": "status"}, autostart=False, want_reply=True)
    if reply:
        print(ok("  running (pid %s, %s frames sent)"
                 % (reply.get("pid"), reply.get("frames_sent"))))
        print("  socket: %s" % reply.get("socket"))
    else:
        print(warn("  not running -- it will start on the next hook event"))

    directory, state_problem = config_module.state_dir_status()
    print("  state dir: %s" % directory)
    if state_problem:
        # Typically ~/.local/state owned by root, courtesy of a past
        # `sudo gem install` or similar. We fall back rather than fail,
        # but the user should know their XDG dir is unusable.
        print(warn("  %s" % state_problem))
        print("             using a temporary directory instead; to fix it:")
        print("               sudo chown -R \"$(id -u)\":\"$(id -g)\" %s"
              % os.path.dirname(config_module.preferred_state_dir()))

    print("\n" + paint("claude code hooks", "1"))
    command = event_command()
    if not os.path.exists(command):
        print(fail("  missing %s" % command))
        problems += 1
    elif not os.access(command, os.X_OK):
        print(fail("  %s is not executable (chmod +x it)" % command))
        problems += 1
    else:
        print(ok("  hook client: %s" % command))
    installed = hooks_module.installed_scopes(getattr(args, "project_dir", None))
    if installed:
        for scope, count in sorted(installed.items()):
            print(ok("  %d hooks in %s scope (%s)"
                     % (count, scope, hooks_module.settings_path(scope))))
    else:
        print(warn("  no hooks installed -- run 'buzerkostka install'"))
        problems += 1

    print()
    if problems:
        print(fail("%d problem%s found" % (problems, "" if problems == 1 else "s")))
        return 1
    print(ok("everything looks healthy"))
    return 0


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buzerkostka",
        description="Drive a Pajenicko BUZERKOSTKA as a Claude Code status light.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, handler, help_text, **kwargs):
        sub_parser = sub.add_parser(name, help=help_text, **kwargs)
        sub_parser.set_defaults(handler=handler)
        return sub_parser

    add("setup", cmd_setup, "interactive first-run configuration")

    p = add("install", cmd_install, "register the Claude Code hooks")
    p.add_argument("--scope", choices=sorted(hooks_module.SCOPES), default="user",
                   help="user (all projects), project (shared) or local (gitignored)")
    p.add_argument("--tier", choices=hooks_module.TIERS, default="standard",
                   help="how many events to listen to")
    p.add_argument("--no-async", action="store_true",
                   help="omit \"async\": true (for older Claude Code builds)")
    p.add_argument("--project-dir", help="project root for project/local scope")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--print-only", action="store_true",
                   help="print the hook JSON instead of installing it")

    p = add("uninstall", cmd_uninstall, "remove the hooks again")
    p.add_argument("--scope", choices=sorted(hooks_module.SCOPES),
                   help="default: every scope")
    p.add_argument("--project-dir")
    p.add_argument("--dry-run", action="store_true")

    p = add("daemon", cmd_daemon, "start the renderer")
    p.add_argument("--foreground", "-f", action="store_true",
                   help="stay attached and log to stderr")

    add("stop", cmd_stop, "stop the renderer")
    add("restart", cmd_restart, "restart the renderer")
    add("reload", cmd_reload, "re-read config.json in the running daemon")

    p = add("status", cmd_status, "show what the light is doing and why")
    p.add_argument("--json", action="store_true")

    p = add("event", cmd_event, "feed one event in by hand")
    p.add_argument("event")
    p.add_argument("--session")

    p = add("state", cmd_state, "force a state (idle, working, permission, ...)")
    p.add_argument("state")
    p.add_argument("--session")

    p = add("color", cmd_color, "show one colour immediately")
    p.add_argument("color", help="#RRGGBB or a name such as 'orange'")
    p.add_argument("--hold", type=float, default=0,
                   help="seconds before normal rendering resumes (0 = until changed)")

    add("off", cmd_off, "clear all sessions and go dark")
    add("pause", cmd_pause, "stop driving the LED without stopping the daemon")
    add("resume", cmd_resume, "undo pause")

    p = add("test", cmd_test, "send red/green/blue/white/off to the cube")
    p.add_argument("--delay", type=float, default=0.8)

    add("calibrate", cmd_calibrate, "detect the cube's channel order")

    p = add("demo", cmd_demo, "play every state so you can see them")
    p.add_argument("states", nargs="*", help="default: the interesting ones")
    p.add_argument("--seconds", type=float, default=4.0,
                   help="how long to hold each non-transient state")

    p = add("discover", cmd_discover, "find the cube's topic on the broker")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--username")
    p.add_argument("--password")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--save", action="store_true", default=True)

    p = add("config", cmd_config, "show or edit the configuration")
    p.add_argument("--path", action="store_true", help="print the config file path")
    p.add_argument("--example", action="store_true", help="print the full defaults")
    p.add_argument("--edit", action="store_true", help="open it in $EDITOR")
    p.add_argument("--show-secrets", action="store_true")

    p = add("doctor", cmd_doctor, "diagnose the whole setup")
    p.add_argument("--project-dir")

    p = add("logs", cmd_logs, "show the daemon log")
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--lines", "-n", type=int, default=40)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except config_module.ConfigError as exc:
        print(fail(str(exc)))
        return 1
    except ValueError as exc:
        print(fail(str(exc)))
        return 1
    except OSError as exc:
        # Unwritable directories, exhausted file descriptors, a full disk.
        # A stack trace helps nobody; say what broke and stop.
        print(fail(str(exc)))
        return 1
    except KeyboardInterrupt:
        return 130
