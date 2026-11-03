"""Scan command-line options, with `.terrahawk.yml` values as defaults."""

import argparse
import os

from . import __version__
from .config import load_config


def parse_args(repo_root):
    """Parse CLI arguments with config file defaults."""
    config = load_config(repo_root)

    parser = argparse.ArgumentParser(
        prog="terrahawk",
        description="\U0001f985 Terrahawk \u2014 Bird's eye view of your infra",
    )
    parser.add_argument("-r", "--root-dir", type=str, default=None,
                        help="Path to the repository root containing the terragrunt structure (default: current directory)")
    parser.add_argument("-p", "--parallelism", type=int, default=int(config.get("parallelism", 6)))
    parser.add_argument("-t", "--timeout", type=int, default=int(config.get("timeout", 300)))
    parser.add_argument("--diagrams", action=argparse.BooleanOptionalAction,
                        default=str(config.get("diagrams", "true")).lower() != "false",
                        help="Embed interactive Mermaid architecture diagrams in the report "
                             "(default: on; use --no-diagrams, or set diagrams=false in .terrahawk.yml, to disable)")
    parser.add_argument("--tags", action="store_true", default=config.get("tags") == "true")
    parser.add_argument("--incremental", action="store_true", default=config.get("incremental") == "true")
    parser.add_argument("--affected", nargs="?", const="main", default=config.get("affected") or None,
                        metavar="BASE",
                        help="Scan only units affected by git changes since BASE (default: main). "
                             "Uses terragrunt's --filter=[BASE...HEAD]; unaffected units are merged from the previous report. "
                             "Terragrunt >=1.0.7 also tracks local module sources / tfr:// registry as read files, "
                             "so edits to a local module correctly mark its consumers affected")
    parser.add_argument("--dag", action=argparse.BooleanOptionalAction,
                        default=str(config.get("dag", "true")).lower() != "false",
                        help="Execute units in dependency-ordered topological waves "
                             "(default: on; use --no-dag, or set dag=false in .terrahawk.yml, to disable)")
    parser.add_argument("--fail-on", choices=["never", "drift", "error", "new"],
                        default=config.get("fail_on", "never"),
                        help="Exit nonzero when the scan finds drift or errors, for CI gating. "
                             "'drift' fails on any drift OR error; 'error' fails only on errors/timeouts; "
                             "'new' fails only on drift/errors that are new since the baseline. "
                             "Ignored drift never fails. Default: never (always exit 0).")
    parser.add_argument("--baseline", type=str, default=config.get("baseline") or None,
                        metavar="PATH|URL",
                        help="Previous JSON report to compare against (new / persisting / resolved "
                             "drift). Default: newest full report in terrahawk_results/. In CI, point "
                             "it at the previous run's published JSON (local path or http(s) URL)")
    parser.add_argument("--ignore-file", type=str, default=config.get("ignore_file") or None,
                        metavar="PATH",
                        help="Known-drift ignore rules, relative to the root dir (default: "
                             ".terrahawkignore when present)")
    parser.add_argument("--codeowners", type=str, default=config.get("codeowners") or None,
                        metavar="PATH",
                        help="CODEOWNERS file for unit owners, relative to the root dir (default: "
                             ".github/CODEOWNERS, CODEOWNERS, docs/CODEOWNERS, .gitlab/CODEOWNERS)")
    parser.add_argument("--sarif", type=str, default=config.get("sarif") or None, metavar="PATH",
                        help="Also write findings as SARIF 2.1.0 (GitHub code scanning annotations)")
    parser.add_argument("--junit", type=str, default=config.get("junit") or None, metavar="PATH",
                        help="Also write a JUnit XML report (GitLab / Azure DevOps / Jenkins test views)")
    parser.add_argument("--notify-on", choices=["new", "findings", "never"],
                        default=config.get("notify_on", "new"),
                        help="When to post the Slack/Teams summary (webhook URL from the "
                             "TERRAHAWK_WEBHOOK_URL env var only). 'new' (default): new drift/errors "
                             "since the baseline; 'findings': any drift/error; 'never'")
    parser.add_argument("--report-url", type=str, default=config.get("report_url") or None,
                        metavar="URL",
                        help="Where the published report lives (linked from alerts). A URL ending in "
                             "'/' gets the report file name appended")
    parser.add_argument("--diagram-assets", choices=["inline", "sidecar"],
                        default=config.get("diagram_assets", "inline"),
                        help="How the Mermaid runtime is delivered in the report. "
                             "'inline' embeds it in the HTML (self-contained, ~3.5MB per report); "
                             "'sidecar' writes mermaid.min.js once beside the report and references it "
                             "(smaller HTML, still offline/air-gapped). Default: inline.")
    parser.add_argument("-u", "--unit", type=str, default=None,
                        help="Scan only the unit whose relative path matches this value (e.g., 'production/westeurope/app-gw')")
    parser.add_argument("--exclude", type=str, default=config.get("exclude", ""))
    parser.add_argument("--no-hooks", action="store_true", default=config.get("no_hooks") == "true",
                        help="Skip before_hook/after_hook/error_hook blocks during plan for a pure read-only "
                             "drift scan (requires Terragrunt >=1.0.8; the 'optional-hooks' experiment flag is "
                             "added automatically below 1.2, where --no-hooks became GA)")
    parser.add_argument("--no-provider-cache", action="store_true",
                        default=config.get("no_provider_cache") == "true",
                        help="Use Terraform's shared plugin cache dir instead of Terragrunt's provider cache "
                             "server (default on Terragrunt 1.x). The plugin dir is not safe for parallel init "
                             "on a cold cache; only use this if the cache server can't reach your registries")
    parser.add_argument("--no-stacks", action="store_true", default=config.get("no_stacks") == "true",
                        help="Skip `terragrunt stack generate` for terragrunt.stack.hcl files. By default stacks "
                             "are auto-generated before discovery so their units are drift-scanned (Terragrunt 1.x)")
    parser.add_argument("--profile", action="append", default=None, metavar="NAME",
                        help="AWS profile to scan with (repeatable). With 2+ profiles, terrahawk maps each "
                             "unit to the profile owning its account (via `aws sts get-caller-identity` + the "
                             "unit's env.hcl aws_account_id), so multiple accounts land in ONE report. "
                             "Config: aws_profiles: a,b in .terrahawk.yml")
    parser.add_argument("--throttle-retries", type=int,
                        default=int(config.get("throttle_retries", 2)), metavar="N",
                        help="Re-plan a unit up to N times with backoff when the provider API "
                             "rate-limits it (Throttling / 429 / TooManyRequests). Default: 2; 0 disables")
    parser.add_argument("--terraform-version", type=str, default=config.get("terraform_version", ""))
    parser.add_argument("--terragrunt-version", type=str, default=config.get("terragrunt_version", ""))
    parser.add_argument("--push-url", type=str, default=config.get("push_url") or None,
                        help="Terrakettle base URL to publish the report to after the scan")
    parser.add_argument("--push-token", type=str,
                        default=config.get("push_token") or os.environ.get("TERRAKETTLE_TOKEN"),
                        help="Terrakettle per-project push token (default: $TERRAKETTLE_TOKEN)")
    parser.add_argument("--version", action="version", version=f"terrahawk {__version__}")
    args = parser.parse_args()
    # Webhook URLs are credentials: they must come from the CI secret store
    # (TERRAHAWK_WEBHOOK_URL), never from a committed config file.
    leaked = [k for k in config if "webhook" in k.lower()]
    if leaked:
        print(f"  \u26a0\ufe0f  Ignoring {', '.join(leaked)} in .terrahawk.yml: webhook URLs are "
              "secrets. Set TERRAHAWK_WEBHOOK_URL from your CI secret store instead, and "
              "rotate the exposed webhook.")
    # `action=append` can't take a config default cleanly; fall back to the
    # comma/space-separated `aws_profiles` from .terrahawk.yml when no --profile.
    if not args.profile and config.get("aws_profiles"):
        args.profile = [p.strip() for p in config["aws_profiles"].replace(",", " ").split() if p.strip()]
    return args

