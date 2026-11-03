"""Shared TUI constants, colours, safe drawing, resource markers and report loading."""

import curses
import json
import os
import sys
from pathlib import Path


# ── Constants ─────────────────────────────────────────────────────

STATUS_ORDER = {"drift": 0, "error": 1, "timeout": 2, "clean": 3}
STATUS_COLORS = {"clean": 1, "drift": 2, "error": 3, "timeout": 4}
ACTION_ORDER = {"create": 0, "replace": 1, "update": 2, "delete": 3, "read": 4}
ACTION_LABELS = {
    "create": "+ Add", "update": "~ Modify", "delete": "- Delete",
    "replace": "* Replace", "read": "? Read",
}
ACTION_COLORS = {"create": 1, "update": 2, "delete": 3, "replace": 4, "read": 5}
_ALL_ACTIONS = ("create", "replace", "update", "delete", "read")
SORT_MODES = ("status", "name", "resources", "duration")


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)     # clean / create
    curses.init_pair(2, curses.COLOR_YELLOW, -1)    # drift / update
    curses.init_pair(3, curses.COLOR_RED, -1)       # error / delete
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)   # timeout / replace
    curses.init_pair(5, curses.COLOR_CYAN, -1)      # info / header / read
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected
    curses.init_pair(7, curses.COLOR_WHITE, -1)     # normal


def _safe(stdscr, y, x, text, maxlen, attr):
    """Write text to screen, ignoring curses errors at boundaries."""
    try:
        stdscr.addnstr(y, x, text, maxlen, attr)
    except curses.error:
        pass


# ── Report loading ────────────────────────────────────────────────

def _find_report(name=None, root_dir=None):
    base = Path(root_dir).resolve() if root_dir else Path.cwd()
    results_dir = base / "terrahawk_results"
    if not results_dir.is_dir():
        print(f"\u274c No terrahawk_results/ directory found in {base}.")
        sys.exit(1)
    if name:
        for candidate in [Path(name), results_dir / name]:
            if candidate.is_file():
                return candidate
        if not name.endswith(".json"):
            candidate = results_dir / f"{name}.json"
            if candidate.is_file():
                return candidate
        matches = sorted(results_dir.glob(f"*{name}*.json"), key=os.path.getmtime, reverse=True)
        if matches:
            return matches[0]
        print(f"\u274c No report matching '{name}' found in {results_dir}")
        sys.exit(1)
    reports = sorted(results_dir.glob("terrahawk_*.json"), key=os.path.getmtime, reverse=True)
    if not reports:
        print(f"\u274c No terrahawk_*.json reports found in {results_dir}")
        sys.exit(1)
    return reports[0]


def _load_report(path):
    with open(path) as f:
        return json.load(f)


def _enc_suffix(resource):
    """Per-resource markers: gzip re-encode only, new since the last run,
    ignored by .terrahawkignore."""
    out = "  [gzip re-encode only]" if resource.get("encodingOnly") else ""
    if resource.get("isNew"):
        out += "  [new]"
    if resource.get("ignored"):
        out += f"  [ignored: {resource['ignored']}]"
    return out
