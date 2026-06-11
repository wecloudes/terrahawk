"""Config hygiene checks: terragrunt hcl validate + hcl format --check."""

import json
import os
import re
import subprocess

from .deps import mise_cmd

_NEEDS_FORMAT_RE = re.compile(r"File '([^']+)' needs formatting")


def check_hygiene(config_dir, tg_ver=""):
    """Run HCL hygiene checks across the config tree.

    Returns {"diagnostics": {rel_path: [issue, ...]}, "unformatted": [rel_path, ...]}
    where rel_path is the file path relative to config_dir. Issues are
    {file, line, severity, summary, detail}. Returns None when the
    terragrunt hcl commands are unavailable (old terragrunt).
    """
    cfg = os.path.realpath(str(config_dir))
    diagnostics = _run_validate(cfg, tg_ver)
    unformatted = _run_format_check(cfg, tg_ver)
    if diagnostics is None and unformatted is None:
        return None
    return {"diagnostics": diagnostics or {}, "unformatted": unformatted or []}


def _rel(path, cfg):
    try:
        return os.path.relpath(os.path.realpath(path), cfg)
    except Exception:
        return path


def _run_validate(cfg, tg_ver=""):
    """`terragrunt hcl validate --json` -> {rel_file: [issues]}. None on failure."""
    cmd = mise_cmd("terragrunt", tg_ver, [
        "hcl", "validate", "--json", f"--working-dir={cfg}",
    ])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    out = r.stdout.strip()
    if not out:
        # Empty output + exit 0 = all valid; anything else = command unsupported
        return {} if r.returncode == 0 else None
    try:
        diags = json.loads(out)
    except Exception:
        return None
    by_file = {}
    for d in diags:
        fname = _rel((d.get("range") or {}).get("filename", ""), cfg)
        by_file.setdefault(fname, []).append({
            "file": fname,
            "line": ((d.get("range") or {}).get("start") or {}).get("line"),
            "severity": d.get("severity", "error"),
            "summary": d.get("summary", ""),
            "detail": (d.get("detail") or "")[:300],
        })
    return by_file


def _run_format_check(cfg, tg_ver=""):
    """`terragrunt hcl format --check` -> [rel_file]. None on failure."""
    cmd = mise_cmd("terragrunt", tg_ver, [
        "hcl", "format", "--check", f"--working-dir={cfg}",
    ])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    files = _NEEDS_FORMAT_RE.findall(r.stdout + r.stderr)
    if r.returncode not in (0, 1):
        return None
    return sorted(_rel(f, cfg) for f in files)


def attach_hygiene(results, hygiene):
    """Annotate report entries with per-unit hygiene info, in place.

    A file belongs to the unit whose rel_path is a prefix of the file's
    directory. Root-level files (root.hcl etc.) match no unit and are
    returned as leftovers: {"diagnostics": [...], "unformatted": [...]}.
    """
    if not hygiene:
        return None
    unit_paths = sorted((r["unit"] for r in results), key=len, reverse=True)

    def find_unit(file_rel):
        d = os.path.dirname(file_rel)
        for up in unit_paths:
            if d == up or d.startswith(up + "/"):
                return up
        return None

    by_unit_diag = {}
    leftover_diag = []
    for fname, issues in hygiene.get("diagnostics", {}).items():
        u = find_unit(fname)
        if u:
            by_unit_diag.setdefault(u, []).extend(issues)
        else:
            leftover_diag.extend(issues)

    by_unit_fmt = {}
    leftover_fmt = []
    for fname in hygiene.get("unformatted", []):
        u = find_unit(fname)
        if u:
            by_unit_fmt.setdefault(u, []).append(fname)
        else:
            leftover_fmt.append(fname)

    for r in results:
        issues = by_unit_diag.get(r["unit"], [])
        fmt = by_unit_fmt.get(r["unit"], [])
        if issues:
            r["hclIssues"] = issues
        if fmt:
            r["unformattedFiles"] = fmt

    if leftover_diag or leftover_fmt:
        return {"diagnostics": leftover_diag, "unformatted": leftover_fmt}
    return None
