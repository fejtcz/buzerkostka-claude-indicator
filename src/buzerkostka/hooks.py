"""Generating and installing the Claude Code hook configuration.

The mapping from Claude Code's hook events to this project's semantic
events is the interesting part, so it lives in one table you can read top
to bottom. Notably :data:`HOOK_SPECS` includes ``PostToolUse`` even in the
``minimal`` tier: the permission prompt fires ``Notification`` /
``PermissionRequest``, and ``PostToolUse`` is the first event afterwards,
so without it the cube would keep blinking orange until the turn ended.

Every hook is registered with ``"async": true``. That is not only about
latency -- an async hook cannot influence a permission decision, which
makes this integration provably observation-only.
"""

from __future__ import annotations

import json
import os
import shlex
import time

TIERS = ("minimal", "standard", "verbose")

#: ``(claude_event, matcher, our_event, tier)``
HOOK_SPECS = (
    # --- the essentials -------------------------------------------------
    ("SessionStart", None, "session-start", "minimal"),
    ("UserPromptSubmit", None, "prompt-submit", "minimal"),
    ("PostToolUse", None, "tool-end", "minimal"),
    ("Notification", "permission_prompt", "permission", "minimal"),
    ("Stop", None, "stop", "minimal"),
    ("SessionEnd", None, "session-end", "minimal"),
    # --- richer signals -------------------------------------------------
    ("PermissionRequest", None, "permission", "standard"),
    ("PermissionDenied", None, "permission-denied", "standard"),
    ("PostToolUseFailure", None, "tool-error", "standard"),
    ("StopFailure", None, "stop-error", "standard"),
    ("Notification", "idle_prompt", "idle-nudge", "standard"),
    ("Notification", "elicitation_dialog", "elicitation", "standard"),
    ("PreCompact", None, "compact-start", "standard"),
    ("PostCompact", None, "compact-end", "standard"),
    # --- one event per tool call; only if you like a busy light --------
    ("PreToolUse", None, "tool-start", "verbose"),
    ("SubagentStart", None, "subagent-start", "verbose"),
    ("SubagentStop", None, "subagent-stop", "verbose"),
    ("TeammateIdle", None, "teammate-idle", "verbose"),
)

#: Any hook whose command contains this substring is considered ours and
#: is replaced on reinstall / dropped on uninstall.
MARKER = "buzerkostka-event"

SCOPES = {
    "user": ("~/.claude/settings.json", None),
    "project": (".claude/settings.json", "project"),
    "local": (".claude/settings.local.json", "project"),
}


def specs_for_tier(tier: str):
    if tier not in TIERS:
        raise ValueError("unknown tier %r (expected %s)" % (tier, ", ".join(TIERS)))
    allowed = TIERS[: TIERS.index(tier) + 1]
    return [spec for spec in HOOK_SPECS if spec[3] in allowed]


def build_hooks(command: str, tier: str = "standard", use_async: bool = True,
                timeout: int = 10) -> dict:
    """Build the ``{"EventName": [group, ...]}`` structure.

    ``command`` is the executable path; it is shell-quoted because Claude
    Code runs command hooks through a shell.
    """
    quoted = shlex.quote(command) if " " in command or "'" in command else command
    hooks = {}
    for claude_event, matcher, our_event, _tier in specs_for_tier(tier):
        handler = {
            "type": "command",
            "command": "%s --event %s" % (quoted, our_event),
            "timeout": timeout,
        }
        if use_async:
            handler["async"] = True
        group = {"hooks": [handler]}
        if matcher:
            group["matcher"] = matcher
        hooks.setdefault(claude_event, []).append(group)
    return hooks


def plugin_hooks(tier: str = "standard", use_async: bool = True) -> dict:
    """The same thing, but for a bundled plugin's ``hooks/hooks.json``."""
    return {
        "hooks": build_hooks(
            '"${CLAUDE_PLUGIN_ROOT}"/bin/buzerkostka-event', tier, use_async
        )
    }


# ----------------------------------------------------------------------
# settings.json surgery
# ----------------------------------------------------------------------
def settings_path(scope: str, project_dir=None) -> str:
    if scope not in SCOPES:
        raise ValueError(
            "unknown scope %r (expected %s)" % (scope, ", ".join(sorted(SCOPES)))
        )
    relative, anchor = SCOPES[scope]
    if anchor == "project":
        base = project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        return os.path.join(base, relative)
    return os.path.expanduser(relative)


def _load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("%s does not contain a JSON object" % path)
    return data


def _write_settings(path: str, data: dict, backup: bool = True):
    """Write settings atomically, keeping one timestamped backup."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    backup_path = None
    if backup and os.path.exists(path):
        backup_path = "%s.buzerkostka-backup-%s" % (path, time.strftime("%Y%m%d%H%M%S"))
        with open(path, "r", encoding="utf-8") as src:
            content = src.read()
        with open(backup_path, "w", encoding="utf-8") as dst:
            dst.write(content)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    return backup_path


def _is_ours(group: dict) -> bool:
    for handler in group.get("hooks") or []:
        command = handler.get("command")
        if isinstance(command, str) and MARKER in command:
            return True
    return False


def strip_ours(settings: dict) -> int:
    """Remove every hook group this project installed. Returns the count."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and _is_ours(group):
                removed += 1
            else:
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]

    if not hooks:
        settings.pop("hooks", None)
    return removed


def install(command: str, scope: str = "user", tier: str = "standard",
            use_async: bool = True, project_dir=None, dry_run: bool = False) -> dict:
    """Register (or re-register) the hooks. Idempotent."""
    path = settings_path(scope, project_dir)
    settings = _load_settings(path)
    removed = strip_ours(settings)

    new_hooks = build_hooks(command, tier, use_async)
    hooks = settings.setdefault("hooks", {})
    for event, groups in new_hooks.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError("hooks.%s in %s is not a list" % (event, path))
        existing.extend(groups)

    added = sum(len(groups) for groups in new_hooks.values())
    backup_path = None
    if not dry_run:
        backup_path = _write_settings(path, settings)

    return {
        "path": path,
        "scope": scope,
        "tier": tier,
        "added": added,
        "replaced": removed,
        "backup": backup_path,
        "events": sorted(new_hooks),
        "dry_run": dry_run,
        "settings": settings,
    }


def uninstall(scope: str = "user", project_dir=None, dry_run: bool = False) -> dict:
    path = settings_path(scope, project_dir)
    if not os.path.exists(path):
        return {"path": path, "scope": scope, "removed": 0, "backup": None,
                "dry_run": dry_run}
    settings = _load_settings(path)
    removed = strip_ours(settings)
    backup_path = None
    if removed and not dry_run:
        backup_path = _write_settings(path, settings)
    return {"path": path, "scope": scope, "removed": removed, "backup": backup_path,
            "dry_run": dry_run}


def installed_scopes(project_dir=None) -> dict:
    """Which scopes currently carry our hooks -> ``{scope: count}``."""
    found = {}
    for scope in SCOPES:
        try:
            path = settings_path(scope, project_dir)
            settings = _load_settings(path)
        except (OSError, ValueError):
            continue
        count = 0
        for groups in (settings.get("hooks") or {}).values():
            if isinstance(groups, list):
                count += sum(
                    1 for g in groups if isinstance(g, dict) and _is_ours(g)
                )
        if count:
            found[scope] = count
    return found
