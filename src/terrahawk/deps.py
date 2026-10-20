"""Dependency checking and version detection."""

import os
import re
import shutil
import subprocess
import sys

TOOLS = {
    "terragrunt": {"required": True,  "url": "https://github.com/gruntwork-io/terragrunt"},
    # Terragrunt drives either engine; one of terraform / tofu is required.
    "terraform":  {"required": True,  "url": "https://github.com/hashicorp/terraform"},
    "tofu":       {"required": False, "url": "https://github.com/opentofu/opentofu"},
    # az (Azure backend), aws (S3 backend) and gcloud (GCS backend) are all
    # optional — the script auto-detects the remote_state backend and skips
    # state-age queries if the matching CLI isn't available.
    "az":         {"required": False, "url": "https://github.com/Azure/azure-cli"},
    "aws":        {"required": False, "url": "https://github.com/aws/aws-cli"},
    "gcloud":     {"required": False, "url": "https://cloud.google.com/sdk/docs/install"},
}


def parse_version(text):
    """Extract a `(major, minor, patch)` tuple from a version string such as
    `v1.2.0-rc3` or `terragrunt version v1.1.6`.

    Pre-release suffixes are ignored (`1.2.0-rc3` -> `(1, 2, 0)`), so a
    release candidate counts as the release it previews. Returns None when
    nothing parses (missing tool, `n/a`).
    """
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def version_at_least(version, minimum):
    """True when a parsed *version* tuple is >= *minimum*; False if unknown."""
    return version is not None and version >= minimum


def uses_mise(args):
    """Return True if mise is needed (version pinning requested)."""
    return bool(args.terraform_version or args.terragrunt_version)


def mise_cmd(tool, version, tool_args, extra=()):
    """Build a command list that runs *tool* at *version* via mise.

    *extra* pins additional tools onto the same `mise exec` PATH (e.g.
    `["terraform@1.16.6"]` so the terraform Terragrunt shells out to is the
    pinned build). If nothing is pinned, the tool is invoked directly.
    """
    pins = ([f"{tool}@{version}"] if version else []) + [p for p in extra if p]
    if not pins:
        return [tool] + list(tool_args)
    return ["mise", "exec", *pins, "--", tool] + list(tool_args)


def tf_cmd(binary, tf_version, tool_args):
    """Build an OpenTofu/Terraform command for *binary*.

    `--terraform-version` pins HashiCorp Terraform, so the mise wrapper only
    applies when the resolved binary is `terraform`.
    """
    if tf_version and os.path.basename(binary) == "terraform":
        return mise_cmd("terraform", tf_version, tool_args)
    return [binary] + list(tool_args)


def pinned_tf_env(args, env):
    """Point Terragrunt at the pinned Terraform via `TG_TF_PATH`.

    Without this, a pinned `--terraform-version` only affected terrahawk's
    own `show`/`output` calls while `terragrunt plan` used whichever tofu /
    terraform was first on PATH. An explicit user choice (`TG_TF_PATH` or the
    legacy `TERRAGRUNT_TFPATH`) is left alone. Mutates and returns *env*.
    """
    if getattr(args, "terraform_version", "") and not (
            env.get("TG_TF_PATH") or env.get("TERRAGRUNT_TFPATH")):
        env["TG_TF_PATH"] = "terraform"
    return env


def resolve_tf_binary(render_json=None, env=None):
    """Return the OpenTofu/Terraform binary Terragrunt runs for a unit.

    Mirrors Terragrunt's own precedence so follow-up `show`/`output` calls
    use the same binary that wrote the plan file and initialised the working
    dir: the `--tf-path` env (`TG_TF_PATH`, legacy `TERRAGRUNT_TFPATH`), then
    the unit's `terraform_binary` attribute (from `terragrunt render`), then
    `tofu` if it is on PATH, otherwise `terraform`.
    """
    env = os.environ if env is None else env
    explicit = env.get("TG_TF_PATH") or env.get("TERRAGRUNT_TFPATH")
    if explicit:
        return explicit
    configured = (render_json or {}).get("terraform_binary")
    if configured:
        return configured
    return "tofu" if shutil.which("tofu") else "terraform"


def check_dependencies(args):
    """Check that required tools are installed."""
    missing_required = False
    print("\n\U0001f50d Checking dependencies...\n")

    # When version pinning is requested, mise is required instead of the
    # individual terraform/terragrunt binaries (mise will install them).
    if uses_mise(args):
        if not shutil.which("mise"):
            print("  ❌ mise is required for version pinning but not installed.")
            print("     Install: https://mise.jdx.dev\n")
            missing_required = True
    else:
        if not shutil.which("terragrunt"):
            print("  ❌ terragrunt is required but not installed.")
            print(f"     Install: {TOOLS['terragrunt']['url']}\n")
            missing_required = True
        if not (shutil.which("terraform") or shutil.which("tofu")):
            print("  ❌ terraform or tofu is required but neither is installed.")
            print(f"     Install: {TOOLS['terraform']['url']} or {TOOLS['tofu']['url']}\n")
            missing_required = True

    # Optional CLIs (cloud backends)
    for cmd in ("az", "aws", "gcloud"):
        pass  # checked lazily at state_age time

    if missing_required:
        print("❌ Missing required dependencies. Please install them and try again.")
        sys.exit(1)


def get_versions(args):
    """Capture tool versions."""
    def _ver(cmd_list, strip_prefix=""):
        try:
            r = subprocess.run(cmd_list, capture_output=True, text=True, timeout=30)
            v = r.stdout.strip().splitlines()[0] if r.stdout.strip() else r.stderr.strip().splitlines()[0]
            if strip_prefix:
                v = v.replace(strip_prefix, "")
            return v.strip()
        except Exception:
            return "n/a"

    tf_bin = resolve_tf_binary(env=pinned_tf_env(args, dict(os.environ)))
    tf_ver = _ver(tf_cmd(tf_bin, args.terraform_version, ["version"]))
    if tf_ver.startswith("OpenTofu v"):
        tf_ver = tf_ver[len("OpenTofu v"):] + " (OpenTofu)"
    else:
        tf_ver = tf_ver.replace("Terraform v", "")

    return {
        "terragrunt": _ver(
            mise_cmd("terragrunt", args.terragrunt_version, ["--version"]),
            strip_prefix="terragrunt version ",
        ),
        "terraform": tf_ver,
    }
