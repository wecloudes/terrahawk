"""CLI entry point: main() split into phase functions."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import load_config, load_unit_timeouts, detect_config_dir
from .deps import check_dependencies, get_versions
from .discovery import discover_units, build_dag
from .incremental import load_manifest, hash_file, save_manifest
from .worker import run_plan
from .process import process_result
from .state_age import query_blob_dates, extract_root_provider_template
from .report import generate_report


def _parse_args(repo_root):
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
    parser.add_argument("--diagrams", action="store_true", default=config.get("diagrams") == "true")
    parser.add_argument("--tags", action="store_true", default=config.get("tags") == "true")
    parser.add_argument("--incremental", action="store_true", default=config.get("incremental") == "true")
    parser.add_argument("--dag", action="store_true", default=config.get("dag") == "true")
    parser.add_argument("-u", "--unit", type=str, default=None,
                        help="Scan only the unit whose relative path matches this value (e.g., 'production/westeurope/app-gw')")
    parser.add_argument("--exclude", type=str, default=config.get("exclude", ""))
    parser.add_argument("--terraform-version", type=str, default=config.get("terraform_version", ""))
    parser.add_argument("--terragrunt-version", type=str, default=config.get("terragrunt_version", ""))
    parser.add_argument("--version", action="version", version=f"terrahawk {__version__}")
    return parser.parse_args()


def _clean_cache(repo_root):
    """Clean terragrunt cache and terraform lock files."""
    print("\U0001f9f9 Cleaning .terragrunt-cache dirs and .terraform.lock.hcl files...")
    subprocess.run(
        'find . -type d -name ".terragrunt-cache" -prune -exec rm -rf {} + ; '
        'find . -type f -name ".terraform.lock.hcl" -delete',
        shell=True, cwd=str(repo_root),
    )


def _setup(repo_root, timestamp):
    """Create output directories and paths."""
    output_dir = repo_root / "terrahawk_results"
    output_dir.mkdir(exist_ok=True)
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json_report = output_dir / f"terrahawk_{timestamp}.json"
    html_report = output_dir / f"terrahawk_{timestamp}.html"
    manifest_path = output_dir / f"terrahawk_{timestamp}.manifest"
    tmp_dir = str(output_dir / f".tmp_{timestamp}")
    os.makedirs(tmp_dir, exist_ok=True)
    return output_dir, report_date, json_report, html_report, manifest_path, tmp_dir


def _apply_incremental(args, units, output_dir):
    """Filter units for incremental mode. Returns (filtered_units, last_json)."""
    last_json = None
    if not args.incremental:
        return units, last_json

    prev_jsons = sorted(output_dir.glob("terrahawk_*.json"), key=os.path.getmtime, reverse=True)
    if prev_jsons:
        last_json = prev_jsons[0]
        prev_manifest_path = str(last_json).replace(".json", ".manifest")
        prev_manifest = load_manifest(prev_manifest_path)
        original_count = len(units)
        units = [(ud, rp) for ud, rp in units if hash_file(os.path.join(ud, "terragrunt.hcl")) != prev_manifest.get(rp, "")]
        skipped = original_count - len(units)
        print(f"  Incremental: {len(units)} changed, {skipped} reused from {last_json.name}")
    else:
        print("  \u26a0\ufe0f  No previous report found, running full scan.")
        args.incremental = False

    return units, last_json


def _execute_plans(units, waves, args, unit_timeouts, repo_root, tmp_dir):
    """Execute plans across all units, respecting DAG waves if enabled."""
    total_to_scan = len(units)
    print(f"\n\U0001f50d Scanning {total_to_scan} units with {args.parallelism} parallel workers...\n")

    raw_results = []

    def execute_batch(batch, start_idx):
        """Run a batch of units in parallel."""
        futures = {}
        with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            for i, (ud, rp) in enumerate(batch):
                idx = start_idx + i
                fut = pool.submit(run_plan, ud, rp, args.timeout, args, unit_timeouts, str(repo_root), tmp_dir, idx)
                futures[fut] = (rp, idx)

            for fut in as_completed(futures):
                rp, idx = futures[fut]
                try:
                    result = fut.result()
                    raw_results.append(result)
                    ec = result.get("exit_code", 1)
                    icon = {0: "\u2705", 2: "\U0001f504", 124: "\u23f1\ufe0f"}.get(ec, "\u274c")
                    label = {0: "No drift", 2: "DRIFT", 124: "TIMEOUT"}.get(ec, "ERROR")
                    print(f"  {icon} {rp:65s} {label}")
                except Exception as e:
                    print(f"  \u274c {rp:65s} EXCEPTION: {e}")

    if total_to_scan == 0:
        print("  Nothing to scan.")
    elif waves:
        idx_offset = 0
        for wave_num, wave in enumerate(waves, 1):
            # Map wave (unit_dirs) back to (unit_dir, rel_path) tuples
            unit_map = {ud: rp for ud, rp in units}
            batch = [(ud, unit_map[ud]) for ud in wave if ud in unit_map]
            if not batch:
                continue
            print(f"\n  \u2500\u2500 Wave {wave_num}/{len(waves)} ({len(batch)} units) \u2500\u2500")
            execute_batch(batch, idx_offset)
            idx_offset += len(batch)
    else:
        execute_batch(units, 0)

    print(f"\n  \u2705 All {total_to_scan} plans completed\n")
    return raw_results


def _assemble_results(raw_results, args, blob_dates, root_provider_tpl, last_json):
    """Process raw results and merge incremental data."""
    print("\U0001f4ca Assembling results...")
    results = [
        process_result(r, args, blob_dates, root_provider_tpl)
        for r in raw_results
    ]

    # Incremental merge
    if args.incremental and last_json and last_json.exists():
        try:
            prev_results = json.load(open(last_json))
            scanned_units = {r["unit"] for r in results}
            merged = 0
            for pr in prev_results:
                if pr["unit"] not in scanned_units:
                    results.append(pr)
                    merged += 1
            if merged:
                print(f"  Merged {merged} unchanged units from previous report")
        except Exception:
            pass

    results.sort(key=lambda r: r["unit"])
    return results


def _write_outputs(results, json_report, html_report, manifest_path, config_dir, report_date, versions, args):
    """Write JSON report, manifest, and HTML report."""
    # Write JSON
    with open(json_report, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Save manifest
    save_manifest(config_dir, str(manifest_path))

    # Generate HTML
    print("\U0001f4dd Generating HTML report...")
    generate_report(results, str(html_report), report_date, versions, args)


def _print_summary(results, total_to_scan, html_report, json_report, args):
    """Print final summary."""
    total = len(results)
    clean = sum(1 for r in results if r["status"] == "clean")
    drift = sum(1 for r in results if r["status"] == "drift")
    error = sum(1 for r in results if r["status"] == "error")
    timeouts = sum(1 for r in results if r["status"] == "timeout")
    merged = total - total_to_scan

    print(f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551               Final Summary              \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

  Total units  : {total} (scanned: {total_to_scan}, merged from previous: {merged})
  \u2705 Clean      : {clean}
  \U0001f504 Drift      : {drift}
  \u274c Errors     : {error}""")

    if timeouts:
        print(f"  \u23f1\ufe0f  Timeouts   : {timeouts}")

    print(f"""
  \U0001f4ca HTML report : {html_report}
  \U0001f4c4 JSON data   : {json_report}
""")


def _cleanup(tmp_dir, repo_root):
    """Clean up temporary files and restore git state."""
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Discard any local changes (modified or untracked) to .terraform.lock.hcl
    # files that terragrunt may have regenerated during the scan.
    subprocess.run(
        "git checkout -- '*.terraform.lock.hcl'",
        shell=True, cwd=str(repo_root), capture_output=True,
    )
    subprocess.run(
        "git ls-files --others --exclude-standard '*.terraform.lock.hcl' -z | xargs -0 rm -f",
        shell=True, cwd=str(repo_root), capture_output=True,
    )

    # Open report
    try:
        subprocess.run(["open", str(repo_root / "terrahawk_results")], capture_output=True)
    except Exception:
        pass


def main():
    # Handle `terrahawk view [report]` subcommand before any other parsing
    if len(sys.argv) > 1 and sys.argv[1] == "view":
        from .tui import run_tui
        report_name = sys.argv[2] if len(sys.argv) > 2 else None
        run_tui(report_name)
        return

    # Pre-parse --root-dir so config file defaults can be loaded from it
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("-r", "--root-dir", type=str, default=None)
    pre_args, _ = pre_parser.parse_known_args()

    if pre_args.root_dir:
        repo_root = Path(pre_args.root_dir).resolve()
    else:
        repo_root = Path.cwd().resolve()

    if not repo_root.is_dir():
        print(f"\u274c Root directory does not exist: {repo_root}")
        sys.exit(1)

    # Clean cache
    _clean_cache(repo_root)

    # Parse arguments
    args = _parse_args(repo_root)

    # Check dependencies
    check_dependencies(args)

    # Setup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir, report_date, json_report, html_report, manifest_path, tmp_dir = _setup(repo_root, timestamp)

    config_dir = detect_config_dir(repo_root)
    unit_timeouts = load_unit_timeouts(repo_root)
    versions = get_versions(args)

    print(f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551          \U0001f985 Terrahawk                   \u2551
\u2551   Bird's eye view of your infra         \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

  Config dir  : {config_dir}
  Parallelism : {args.parallelism}
  Timeout     : {args.timeout}s per unit
  Terraform   : {args.terraform_version or 'default (system)'}
  Terragrunt  : {args.terragrunt_version or 'default (system)'}
  Diagrams    : {args.diagrams}
  Tags        : {args.tags}
  Incremental : {args.incremental}
  DAG         : {args.dag}
  HTML report : {html_report}
""")

    # Discover units
    print("\U0001f4cb Discovering units...")
    units = discover_units(config_dir, args.exclude)
    total_discovered = len(units)
    print(f"  Found {total_discovered} units")

    if args.exclude:
        print(f"  Exclude pattern: {args.exclude}")

    if args.unit:
        needle = args.unit.strip("/")
        units = [(ud, rp) for ud, rp in units if rp == needle or rp.endswith("/" + needle)]
        if not units:
            print(f"\n  \u274c No unit found matching '{args.unit}'")
            print(f"     Run without --unit to list all discovered units.")
            sys.exit(1)
        print(f"  Single-unit mode: {units[0][1]}")

    # Incremental mode
    units, last_json = _apply_incremental(args, units, output_dir)
    total_to_scan = len(units)

    # Query state ages
    print("\U0001f4c5 Querying state file ages...")
    blob_dates = query_blob_dates(config_dir)

    # Extract global provider template from root terragrunt.hcl
    root_provider_tpl = extract_root_provider_template(config_dir)

    # Build DAG if enabled
    waves = None
    if args.dag and units:
        print("\U0001f517 Building dependency DAG...")
        waves = build_dag(units)
        print(f"  {len(waves)} execution waves from {len(units)} units")

    # Run plans
    raw_results = _execute_plans(units, waves, args, unit_timeouts, repo_root, tmp_dir)

    # Process results
    results = _assemble_results(raw_results, args, blob_dates, root_provider_tpl, last_json)

    # Write outputs
    _write_outputs(results, json_report, html_report, manifest_path, config_dir, report_date, versions, args)

    # Summary
    _print_summary(results, total_to_scan, html_report, json_report, args)

    # Cleanup
    _cleanup(tmp_dir, repo_root)
