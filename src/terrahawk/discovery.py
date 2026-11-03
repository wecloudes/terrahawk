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

# Above this many `--unit` candidates, pre-narrowing is skipped: hundreds of
# repeated `--filter` args bloat the command line (ARG_MAX on huge repos) and
# a broad needle saves little of the walk anyway.
MAX_UNIT_FILTERS = 50


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


def discover_units(config_dir, exclude_pattern="", tg_ver="", filter_expr=None,
                   unit_filters=None):
    """Find all terragrunt units.

    Tries `terragrunt find --format json --dependencies` first (Terragrunt 1.x):
    native HCL parsing catches `dependencies { paths }` blocks, include-based
    dependencies, and stacks that the regex fallback misses.

    filter_expr is a terragrunt filter query (e.g. "[main...HEAD]" for
    git-affected units) — native discovery only; raises RuntimeError when
    a filter is requested but native discovery is unavailable.

    unit_filters is a list of `./rel_path` path-filter expressions for
    `--unit` pre-narrowing (see unit_path_filters): Terragrunt ≥1.2 narrows
    its filesystem walk to them. Native discovery only; when terragrunt
    rejects them the find is retried unfiltered (never a silent fallback).

    Returns (units, deps) where units is a list of (unit_dir, rel_path) and
    deps is {unit_dir: set(dependency_unit_dirs)} from native discovery, or
    None when the rglob fallback was used.
    """
    native = _discover_native(config_dir, tg_ver, filter_expr, unit_filters)
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


def unit_path_filters(config_dir, unit_arg):
    """`./rel_path` filter expressions pre-narrowing discovery for `--unit`.

    Terragrunt ≥1.2 analyzes `--filter` path expressions before discovery
    and skips most of the filesystem walk for them — but a bare `--unit`
    value can match a unit anywhere (suffix match), which cannot narrow
    anything. This globs `terragrunt.hcl` files with a cheap filesystem
    walk (no HCL parsing, `.terragrunt-cache` pruned; `.terragrunt-stack`
    members included, matching what native `find` discovers) using the
    exact/suffix rule cli.py applies after discovery, and returns one
    `./rel_path` expression per candidate unit.

    Returns None when nothing matches or the needle is unusable — the
    caller then runs discovery unfiltered and the post-filter reports the
    usual "No unit found" error.
    """
    needle = (unit_arg or "").strip().strip("/")
    if not needle:
        return None
    matches = []
    # followlinks: a symlinked unit dir must be a candidate, or narrowing
    # would silently drop it whenever some other unit matches. Aliases of the
    # same real dir are all kept (each is a distinct rel_path to find); only
    # links pointing back at an ancestor (cycles) are pruned.
    for root, dirs, files in os.walk(config_dir, followlinks=True):
        real_root = os.path.realpath(root)
        # Prune only the cache tree (it can hold thousands of module files
        # after a scan); stack members under .terragrunt-stack are real units.
        dirs[:] = [d for d in dirs if d != ".terragrunt-cache"
                   and not _links_to_ancestor(os.path.join(root, d), real_root)]
        if "terragrunt.hcl" not in files:
            continue
        rel = os.path.relpath(root, config_dir).replace(os.sep, "/")
        if rel in (".", ""):
            continue  # root config, never a unit
        if rel == needle or rel.endswith("/" + needle):
            matches.append(f"./{rel}")
    return sorted(set(matches)) or None


def _links_to_ancestor(path, real_parent):
    """True when `path` is a symlink resolving to `real_parent` or one of its
    ancestors — following it would loop forever."""
    if not os.path.islink(path):
        return False
    target = os.path.realpath(path)
    return real_parent == target or real_parent.startswith(target.rstrip(os.sep) + os.sep)


def _find_json(config_dir, tg_ver, filter_expr, unit_filters):
    """Run `terragrunt find --format json --dependencies` once.

    Returns (data, timed_out): data is the parsed JSON list, or None on any
    failure (nonzero exit, empty stdout, bad JSON, timeout); timed_out tells
    the caller a retry would just burn another full timeout."""
    cfg = os.path.realpath(str(config_dir))
    find_args = [
        "find", "--format", "json", "--dependencies",
        f"--working-dir={cfg}",
    ]
    if filter_expr:
        find_args.append(f"--filter={filter_expr}")
    for uf in unit_filters or []:
        find_args.append(f"--filter={uf}")
    cmd = mise_cmd("terragrunt", tg_ver, find_args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not r.stdout.strip():
            return None, False
        return json.loads(r.stdout), False
    except subprocess.TimeoutExpired:
        return None, True
    except Exception:
        return None, False


def _discover_native(config_dir, tg_ver="", filter_expr=None, unit_filters=None):
    """Discover units via `terragrunt find`. Returns (units, deps) or None on failure."""
    # realpath BEFORE invoking find: terragrunt emits dependency paths
    # relative to the working dir as given, computed lexically — resolving
    # symlinks afterwards would corrupt them (e.g. /tmp -> /private/tmp).
    data, timed_out = _find_json(config_dir, tg_ver, filter_expr, unit_filters)
    if unit_filters and not timed_out and not any(
            isinstance(e, dict) and e.get("type") == "unit" for e in data or []):
        # Narrowing must never change the outcome. Retry unfiltered when
        # terragrunt rejects a path filter (odd characters in a dir name, an
        # older find) — rather than degrading to the rglob fallback, which
        # loses `dependencies { paths }` — or when it accepts the filters but
        # they match no unit (path-filter semantics differing from our
        # glob, e.g. on symlinks or dot-dirs). A timeout is not retried: a
        # second full walk would just time out again.
        data, _ = _find_json(config_dir, tg_ver, filter_expr, None)
    if data is None:
        return None

    # realpath BEFORE the walk below: paths must be resolved the same way
    # _find_json resolved the working dir (symlink-consistent, e.g. /tmp ->
    # /private/tmp), or dependency paths stop matching across symlinks.
    cfg = os.path.realpath(str(config_dir))
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
