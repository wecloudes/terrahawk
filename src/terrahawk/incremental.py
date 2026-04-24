"""Incremental mode: manifest loading, hashing, and saving."""

import hashlib
import os


def load_manifest(manifest_path):
    """Load previous manifest (unit_path -> md5)."""
    manifest = {}
    if manifest_path and os.path.exists(manifest_path):
        for line in open(manifest_path):
            line = line.strip()
            if ":" in line:
                rp, h = line.rsplit(":", 1)
                manifest[rp] = h
    return manifest


def hash_file(path):
    """MD5 hash of a file's content."""
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except Exception:
        return "new"


def save_manifest(config_dir, manifest_path):
    """Save manifest of all terragrunt.hcl files."""
    with open(manifest_path, "w") as f:
        for tg in sorted(config_dir.rglob("terragrunt.hcl")):
            if ".terragrunt-cache" in str(tg):
                continue
            if tg.parent == config_dir:
                continue
            rp = str(tg.parent.relative_to(config_dir))
            h = hash_file(str(tg))
            f.write(f"{rp}:{h}\n")
