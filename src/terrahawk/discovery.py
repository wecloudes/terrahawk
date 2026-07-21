"""Unit discovery and DAG builder."""

import json
import os
import re
import subprocess
from collections import defaultdict, deque

from .deps import mise_cmd


def discover_units(config_dir, exclude_pattern="", tg_ver="", filter_expr=None):
    """Find all terragrunt units.

    Tries `terragrunt find --format json --dependencies` first (Terragrunt 1.x):
    native HCL parsing catches `dependencies { paths }` blocks, include-based
    dependencies, and stacks that the regex fallback misses.

    filter_expr is a terragrunt filter query (e.g. "[main...HEAD]" for
    git-affected units) — native discovery only; raises RuntimeError when
    a filter is requested but native discovery is unavailable.

    Returns (units, deps) where units is a list of (unit_dir, rel_path) and
    deps is {unit_dir: set(dependency_unit_dirs)} from native discovery, or
    None when the rglob fallback was used.
    """
    native = _discover_native(config_dir, tg_ver, filter_expr)
    if native is not None:
        units, deps = native
    elif filter_expr:
        raise RuntimeError(
            "--affected requires `terragrunt find --filter` (Terragrunt 1.x) "
            "and a git repository; native discovery failed."
        )
    else:
        units, deps = _discover_rglob(config_dir), None

    if exclude_pattern:
        units = [(ud, rp) for ud, rp in units if not re.search(exclude_pattern, rp)]

    return units, deps


def _discover_native(config_dir, tg_ver="", filter_expr=None):
    """Discover units via `terragrunt find`. Returns (units, deps) or None on failure."""
    # realpath BEFORE invoking find: terragrunt emits dependency paths
    # relative to the working dir as given, computed lexically — resolving
    # symlinks afterwards would corrupt them (e.g. /tmp -> /private/tmp).
    cfg = os.path.realpath(str(config_dir))
    find_args = [
        "find", "--format", "json", "--dependencies",
        f"--working-dir={cfg}",
    ]
    if filter_expr:
        find_args.append(f"--filter={filter_expr}")
    cmd = mise_cmd("terragrunt", tg_ver, find_args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None

    units = []
    deps = {}
    for entry in data:
        if entry.get("type") != "unit":
            continue
        rel_path = entry["path"]
        unit_dir = os.path.normpath(os.path.join(cfg, rel_path))
        units.append((unit_dir, rel_path))
        # Dependency paths are relative to the working dir
        deps[unit_dir] = {
            os.path.normpath(os.path.join(cfg, d))
            for d in entry.get("dependencies", [])
        }
    return sorted(units, key=lambda u: u[1]), deps


def _discover_rglob(config_dir):
    """Fallback discovery: glob for terragrunt.hcl files (pre-1.x terragrunt)."""
    units = []
    for tg_file in sorted(config_dir.rglob("terragrunt.hcl")):
        # Skip generated artifacts: .terragrunt-cache (working dirs) and
        # .terragrunt-stack (units materialised from terragrunt.stack.hcl).
        # Stacks are a 1.x feature handled by the native `find` path; on the
        # pre-1.x fallback these dirs are only stale leftovers.
        if ".terragrunt-cache" in str(tg_file) or ".terragrunt-stack" in str(tg_file):
            continue
        unit_dir = tg_file.parent
        if unit_dir == config_dir:
            continue  # skip root
        rel_path = str(unit_dir.relative_to(config_dir))
        units.append((os.path.realpath(str(unit_dir)), rel_path))
    return units


def _parse_deps_regex(units):
    """Regex fallback: extract dependency config_paths from terragrunt.hcl files.

    Only matches `dependency "x" { config_path = "..." }` blocks; misses
    `dependencies { paths }` and include-based deps. Used when native
    discovery is unavailable.
    """
    all_dirs = set(ud for ud, _ in units)
    deps = defaultdict(set)
    for ud, rp in units:
        tg_path = os.path.join(ud, "terragrunt.hcl")
        try:
            content = open(tg_path).read()
            for m in re.finditer(r'dependency\s+"[^"]+"\s*\{[^}]*?config_path\s*=\s*"([^"]+)"', content, re.DOTALL):
                dep_abs = os.path.realpath(os.path.normpath(os.path.join(ud, m.group(1))))
                if dep_abs in all_dirs:
                    deps[ud].add(dep_abs)
        except Exception:
            pass
    return deps


def build_dag(units, deps=None):
    """Build execution waves from unit dependencies (Kahn's algorithm).

    deps comes from native discovery when available; otherwise dependency
    blocks are re-parsed with the regex fallback.
    """
    all_dirs = set(ud for ud, _ in units)
    if deps is None:
        deps = _parse_deps_regex(units)

    # Kahn's algorithm (only consider deps between scanned units)
    in_deg = defaultdict(int)
    reverse = defaultdict(set)
    for ud in all_dirs:
        for dep in deps.get(ud, ()):
            if dep not in all_dirs:
                continue
            in_deg[ud] += 1
            reverse[dep].add(ud)

    waves = []
    queue = deque(ud for ud in all_dirs if in_deg[ud] == 0)
    done = set()
    while queue:
        wave = list(queue)
        waves.append(wave)
        done.update(wave)
        next_q = deque()
        for ud in wave:
            for dependent in reverse[ud]:
                in_deg[dependent] -= 1
                if in_deg[dependent] == 0:
                    next_q.append(dependent)
        queue = next_q

    # Catch circular deps
    remaining = [ud for ud in all_dirs if ud not in done]
    if remaining:
        waves.append(remaining)

    return waves
