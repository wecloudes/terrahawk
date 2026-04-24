"""Configuration file loading and config directory detection."""

import sys
from pathlib import Path


def load_config(script_dir):
    """Load .terrahawk.yml config file (simple key-value parser)."""
    config = {}
    config_file = script_dir / ".terrahawk.yml"
    if not config_file.exists():
        return config
    for line in config_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue  # skip nested (unit_timeouts values)
        key, val = line.split(":", 1)
        config[key.strip()] = val.strip().strip("'\"")
    return config


def load_unit_timeouts(script_dir):
    """Parse unit_timeouts section from .terrahawk.yml."""
    timeouts = {}
    config_file = script_dir / ".terrahawk.yml"
    if not config_file.exists():
        return timeouts
    in_section = False
    for line in config_file.read_text().splitlines():
        stripped = line.strip()
        if stripped == "unit_timeouts:":
            in_section = True
            continue
        if in_section:
            if not line.startswith(" ") and not line.startswith("\t"):
                break
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                try:
                    timeouts[parts[0].strip()] = int(parts[1].strip())
                except ValueError:
                    pass
    return timeouts


def detect_config_dir(script_dir):
    """Auto-detect the terragrunt config root directory."""
    for candidate in [
        script_dir / "terragrunt" / "config",
        script_dir / "terragrunt",
        script_dir / "config",
        script_dir,
    ]:
        if candidate.is_dir():
            has_root = list(candidate.glob("root.hcl")) + list(candidate.glob("terragrunt.hcl"))
            if has_root:
                return candidate

    # Fallback: find common ancestor of all terragrunt.hcl files
    tg_files = list(script_dir.rglob("terragrunt.hcl"))
    tg_files = [f for f in tg_files if ".terragrunt-cache" not in str(f)]
    if not tg_files:
        print("\u274c No terragrunt.hcl files found.")
        sys.exit(1)
    config_dir = tg_files[0].parent
    while config_dir != script_dir and config_dir != config_dir.parent:
        count = sum(1 for _ in config_dir.rglob("terragrunt.hcl") if ".terragrunt-cache" not in str(_))
        if count > 1:
            break
        config_dir = config_dir.parent
    return config_dir
