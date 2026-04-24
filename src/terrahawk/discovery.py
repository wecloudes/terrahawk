"""Unit discovery and DAG builder."""

import os
import re
from collections import defaultdict, deque


def discover_units(config_dir, exclude_pattern=""):
    """Find all terragrunt units (directories with terragrunt.hcl)."""
    units = []
    for tg_file in sorted(config_dir.rglob("terragrunt.hcl")):
        if ".terragrunt-cache" in str(tg_file):
            continue
        unit_dir = tg_file.parent
        if unit_dir == config_dir:
            continue  # skip root
        rel_path = str(unit_dir.relative_to(config_dir))
        units.append((str(unit_dir), rel_path))

    if exclude_pattern:
        units = [(ud, rp) for ud, rp in units if not re.search(exclude_pattern, rp)]

    return units


def build_dag(units):
    """Parse dependency blocks and build execution waves."""
    dir_map = {ud: rp for ud, rp in units}
    all_dirs = set(ud for ud, _ in units)
    deps = defaultdict(set)

    for ud, rp in units:
        tg_path = os.path.join(ud, "terragrunt.hcl")
        try:
            content = open(tg_path).read()
            for m in re.finditer(r'dependency\s+"[^"]+"\s*\{[^}]*?config_path\s*=\s*"([^"]+)"', content, re.DOTALL):
                dep_abs = os.path.normpath(os.path.join(ud, m.group(1)))
                if dep_abs in all_dirs:
                    deps[ud].add(dep_abs)
        except Exception:
            pass

    # Kahn's algorithm
    in_deg = defaultdict(int)
    reverse = defaultdict(set)
    for ud in all_dirs:
        for dep in deps[ud]:
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
