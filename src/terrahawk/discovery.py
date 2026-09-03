"""Unit discovery and DAG builder."""

import json
import os
import re
import subprocess
from collections import defaultdict, deque

from .deps import mise_cmd

# Terragrunt's own generated dot-dirs. They start with "." but hold real,
# scannable units (stack members) or working copies — exempt from the
# hidden-dir skip rule below.
_TG_GENERATED_DIRS = {".terragrunt-stack", ".terragrunt-cache"}

# Directory-name segments whose presence anywhere in a unit's rel_path always
# marks it non-scannable, regardless of --exclude:
#   - "catalog": holds reusable module wrappers (a module catalog), which are
#     templates for stacks, not deployable units.
_ALWAYS_SKIP_SEGMENTS = {"catalog"}


def _is_always_skipped(rel_path):
    """True when rel_path lives under a hidden dir or a module catalog.

    Hidden dirs (any path segment starting with "." — e.g. .migration-backup,
    .git, .idea) hold backups/VCS/tooling, never live units. Terragrunt's own
    generated dot-dirs (.terragrunt-stack / .terragrunt-cache) are exempt: stack
    members are real units. `catalog` segments are module catalogs, not units.
    Applied to native + rglob discovery alike, before --exclude.
    """
    for seg in rel_path.replace("\\", "/").split("/"):
        if seg in _TG_GENERATED_DIRS:
            continue
        if seg.startswith("."):
            return True
        if seg in _ALWAYS_SKIP_SEGMENTS:
            return True
    return False


def find_stack_files(config_dir):
    """Return dirs holding a *source* terragrunt.stack.hcl.

    Excludes generated artifacts (`.terragrunt-cache`, `.terragrunt-stack`) so
    only author-written stack roots are returned — nested stacks materialised
    under `.terragrunt-stack` are handled recursively by `stack generate`.
    """
    stack_dirs = []
    for sf in sorted(config_dir.rglob("terragrunt.stack.hcl")):
        s = str(sf)
        if ".terragrunt-cache" in s or ".terragrunt-stack" in s:
            continue
        stack_dirs.append(sf.parent)
    return stack_dirs


def generate_stacks(config_dir, tg_ver=""):
    """Materialise explicit stacks (terragrunt.stack.hcl) into `.terragrunt-stack`.

    Runs `terragrunt stack generate` in every directory that holds a source
    terragrunt.stack.hcl, so the generated units become discoverable by
    `terragrunt find` (as `type=unit`) and plannable by the worker. Requires
    Terragrunt 1.x; older versions error and are reported as failures.

    Returns (n_stacks, n_failed): number of stack roots found and how many
    failed to generate. (0, 0) when no stacks exist (no-op).
    """
    stack_dirs = find_stack_files(config_dir)
    if not stack_dirs:
        return (0, 0)
    failed = 0
    for sd in stack_dirs:
        cmd = mise_cmd("terragrunt", tg_ver, [
            "stack", "generate",
            f"--working-dir={os.path.realpath(str(sd))}",
        ])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                failed += 1
                lines = (r.stderr or r.stdout).strip().splitlines()
                tail = lines[-1] if lines else "unknown error"
                print(f"  ⚠ stack generate failed in {sd}: {tail}")
        except Exception as e:
            failed += 1
            print(f"  ⚠ stack generate errored in {sd}: {e}")
    return (len(stack_dirs), failed)


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

    # Drop the repository root's own terragrunt.hcl (discovered as rel_path
    # "." / ""): it has no region.hcl/env.hcl above it, so it always errors.
    units = [(ud, rp) for ud, rp in units if rp not in (".", "")]

    # Always drop hidden dirs (.migration-backup, .git, …) and module catalogs.
    units = [(ud, rp) for ud, rp in units if not _is_always_skipped(rp)]

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
