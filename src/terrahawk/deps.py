"""Dependency checking and version detection."""

import shutil
import subprocess
import sys

TOOLS = {
    "terragrunt": {"required": True,  "url": "https://github.com/gruntwork-io/terragrunt"},
    "terraform":  {"required": True,  "url": "https://github.com/hashicorp/terraform"},
    # az (Azure backend), aws (S3 backend) and gcloud (GCS backend) are all
    # optional — the script auto-detects the remote_state backend and skips
    # state-age queries if the matching CLI isn't available.
    "az":         {"required": False, "url": "https://github.com/Azure/azure-cli"},
    "aws":        {"required": False, "url": "https://github.com/aws/aws-cli"},
    "gcloud":     {"required": False, "url": "https://cloud.google.com/sdk/docs/install"},
}


def uses_mise(args):
    """Return True if mise is needed (version pinning requested)."""
    return bool(args.terraform_version or args.terragrunt_version)


def mise_cmd(tool, version, tool_args):
    """Build a command list that runs *tool* at *version* via mise.

    If *version* is empty, the tool is invoked directly (no mise wrapper).
    """
    if not version:
        return [tool] + list(tool_args)
    return ["mise", "exec", f"{tool}@{version}", "--"] + [tool] + list(tool_args)


def check_dependencies(args):
    """Check that required tools are installed."""
    missing_required = False
    print("\n\U0001f50d Checking dependencies...\n")

    # When version pinning is requested, mise is required instead of the
    # individual terraform/terragrunt binaries (mise will install them).
    if uses_mise(args):
        if not shutil.which("mise"):
            print("  \u274c mise is required for version pinning but not installed.")
            print("     Install: https://mise.jdx.dev\n")
            missing_required = True
    else:
        for cmd in ("terraform", "terragrunt"):
            if not shutil.which(cmd):
                print(f"  \u274c {cmd} is required but not installed.")
                print(f"     Install: {TOOLS[cmd]['url']}\n")
                missing_required = True

    # Optional CLIs (cloud backends)
    for cmd in ("az", "aws", "gcloud"):
        pass  # checked lazily at state_age time

    if missing_required:
        print("\u274c Missing required dependencies. Please install them and try again.")
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

    return {
        "terragrunt": _ver(
            mise_cmd("terragrunt", args.terragrunt_version, ["--version"]),
            strip_prefix="terragrunt version ",
        ),
        "terraform": _ver(
            mise_cmd("terraform", args.terraform_version, ["version"]),
            strip_prefix="Terraform v",
        ),
    }
