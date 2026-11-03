"""Scan-less subcommands: `terrahawk view`, `terrahawk graph`, `terrahawk list`."""

import argparse
import json
import os
import sys
from pathlib import Path

from .config import detect_config_dir
from .discovery import discover_units
from .graph import cmd_graph


def run_view(argv):
    """`terrahawk view [report] [-r DIR]` — browse a report in the terminal."""
    from .tui import run_tui  # curses import deferred to the subcommand
    vp = argparse.ArgumentParser(prog="terrahawk view",
                                 description="Browse a scan report in the terminal")
    vp.add_argument("report", nargs="?", default=None)
    vp.add_argument("-r", "--root-dir", type=str, default=None)
    va = vp.parse_args(argv)
    run_tui(va.report, va.root_dir)
    return 0


def resolve_subcmd_root(root_dir):
    """Resolve repo root + config dir for the scan-less subcommands."""
    repo_root = Path(root_dir).resolve() if root_dir else Path.cwd().resolve()
    if not repo_root.is_dir():
        print(f"❌ Root directory does not exist: {repo_root}")
        sys.exit(1)
    return repo_root, detect_config_dir(repo_root)


def run_graph(argv):
    """`terrahawk graph [-r DIR] [-o FILE.html]`."""
    p = argparse.ArgumentParser(prog="terrahawk graph",
                                description="Print the unit dependency graph as Mermaid (or write HTML)")
    p.add_argument("-r", "--root-dir", type=str, default=None)
    p.add_argument("-o", "--output", type=str, default=None,
                   help="Write a self-contained HTML file instead of printing Mermaid")
    p.add_argument("--terragrunt-version", type=str, default="")
    a = p.parse_args(argv)
    _, config_dir = resolve_subcmd_root(a.root_dir)
    return cmd_graph(config_dir, a.terragrunt_version, a.output)


def run_list(argv):
    """`terrahawk list [-r DIR]` — unit inventory + last report status, no scan."""
    p = argparse.ArgumentParser(prog="terrahawk list",
                                description="List discovered units with last-report status (no scan)")
    p.add_argument("-r", "--root-dir", type=str, default=None)
    p.add_argument("--terragrunt-version", type=str, default="")
    a = p.parse_args(argv)
    repo_root, config_dir = resolve_subcmd_root(a.root_dir)

    units, deps = discover_units(config_dir, tg_ver=a.terragrunt_version)
    if not units:
        print("No units found.")
        return 1

    # Last report (if any) for status/age/resources
    last = {}
    last_name = ""
    output_dir = repo_root / "terrahawk_results"
    if output_dir.exists():
        prev = sorted(output_dir.glob("terrahawk_*.json"), key=os.path.getmtime, reverse=True)
        if prev:
            try:
                last = {e["unit"]: e for e in json.load(open(prev[0]))}
                last_name = prev[0].name
            except Exception:
                pass

    dep_counts = {}
    if deps:
        dir_to_rel = {ud: rp for ud, rp in units}
        for ud, rp in units:
            dep_counts[rp] = len([d for d in deps.get(ud, ()) if d in dir_to_rel])

    icons = {"clean": "✅", "drift": "\U0001f504", "error": "❌", "timeout": "⏱️"}
    w = max(len(rp) for _, rp in units)
    hdr = f"  {'UNIT':<{w}}  {'DEPS':>4}  {'STATUS':<9} {'AGE':>5}  {'RES':>4}  {'DUR':>6}"
    print(f"\U0001f4cb {len(units)} units" + (f"  (last report: {last_name})" if last_name else "  (no previous report)"))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for _, rp in units:
        e = last.get(rp, {})
        st = e.get("status", "-")
        icon = icons.get(st, " ")
        age = f"{e['stateAgeDays']}d" if e.get("stateAgeDays") is not None else "-"
        res = e.get("resourceCount") or "-"
        dur = f"{e['duration']:.0f}s" if e.get("duration") is not None else "-"
        print(f"  {rp:<{w}}  {dep_counts.get(rp, 0):>4}  {icon} {st:<7} {age:>5}  {res:>4}  {dur:>6}")
    return 0

