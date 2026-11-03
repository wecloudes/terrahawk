"""CLI entry point: subcommand dispatch and the scan orchestrator.

The scan reads top to bottom as a list of phases (`phases.py`); argument
parsing lives in `options.py`, the scan-less subcommands in `commands.py`,
and post-scan enrichment (owners, ignore rules, run-over-run trend) in
`findings.py`.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from . import phases
from .commands import run_graph, run_list, run_view
from .config import load_unit_timeouts, detect_config_dir
from .deps import check_dependencies, get_versions, parse_version
from .export import write_sarif, write_junit
from .findings import annotate_findings, exit_code
from .notify import maybe_notify
from .options import parse_args
from .process import build_stack_graphs
from .push import maybe_push
from .state_age import extract_root_provider_template

SUBCOMMANDS = {"view": run_view, "graph": run_graph, "list": run_list}


def _repo_root_from_argv():
    """Pre-parse --root-dir so .terrahawk.yml defaults load from the right place."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-r", "--root-dir", type=str, default=None)
    pre_args, _ = pre.parse_known_args()
    repo_root = Path(pre_args.root_dir).resolve() if pre_args.root_dir else Path.cwd().resolve()
    if not repo_root.is_dir():
        print(f"❌ Root directory does not exist: {repo_root}")
        sys.exit(1)
    return repo_root


def scan():
    """Run a full drift scan. Returns the process exit code."""
    repo_root = _repo_root_from_argv()
    phases.clean_cache(repo_root)
    args = parse_args(repo_root)
    check_dependencies(args)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir, report_date, json_report, html_report, manifest_path, tmp_dir = \
        phases.setup(repo_root, timestamp)
    config_dir = detect_config_dir(repo_root)
    unit_timeouts = load_unit_timeouts(repo_root)
    versions = get_versions(args)
    # Parsed Terragrunt version gates flags that graduated from experiments
    # (worker._build_plan_args); None when unknown → legacy flags are kept.
    args.tg_version = parse_version(versions.get("terragrunt"))
    phases.print_banner(args, config_dir, html_report, versions)

    # Discover and select units
    generated_stacks = phases.materialise_stacks(args, config_dir)
    units, native_deps, dir_by_relpath = phases.discover(args, config_dir)
    units, last_json = phases.apply_incremental(args, units, output_dir)
    total_to_scan = len(units)

    # Per-run context for the plans
    state_profile = phases.resolve_profiles(args, units, config_dir)
    hygiene = phases.run_hygiene(args, config_dir)
    blob_dates = phases.query_state_ages(config_dir, state_profile)
    root_provider_tpl = extract_root_provider_template(config_dir)
    waves = phases.build_waves(args, units, native_deps)

    # Plan and process
    raw_results = phases.execute_plans(units, waves, args, unit_timeouts, repo_root, tmp_dir)
    results = phases.assemble_results(raw_results, args, blob_dates, root_provider_tpl, last_json)
    phases.apply_hygiene(results, hygiene)
    trend = annotate_findings(results, args, repo_root, config_dir, output_dir, json_report)
    stack_graphs = build_stack_graphs(results, native_deps, dir_by_relpath)
    if stack_graphs:
        print(f"\U0001f5fa️  Built {len(stack_graphs)} stack diagram(s)")

    # Outputs
    phases.write_outputs(results, json_report, html_report, manifest_path, config_dir,
                         report_date, versions, args, stack_graphs, meta={"trend": trend})
    if args.sarif:
        n = write_sarif(results, args.sarif, repo_root, config_dir, __version__)
        print(f"\U0001f4e4 SARIF: {n} result(s) → {args.sarif}")
    if args.junit:
        n = write_junit(results, args.junit)
        print(f"\U0001f4e4 JUnit: {n} test case(s) → {args.junit}")
    phases.print_summary(results, total_to_scan, html_report, json_report, args, trend)

    # Delivery: Slack/Teams alert (URL from TERRAHAWK_WEBHOOK_URL only), Terrakettle
    maybe_notify(results, trend["counts"], args.notify_on, args.report_url or "",
                 Path(html_report).name, trend["baseline"])
    maybe_push(args, json_report, html_report)

    phases.cleanup(tmp_dir, repo_root, clean_stacks=generated_stacks)
    return exit_code(results, trend["counts"], args.fail_on)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        rc = SUBCOMMANDS[sys.argv[1]](sys.argv[2:])
        # `view` returns after the curses session; graph/list report status.
        if sys.argv[1] != "view":
            sys.exit(rc)
        return None
    return scan()
