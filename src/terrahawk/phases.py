"""Scan pipeline phases, called in order by `cli.scan()`.

Each phase prints its own progress line and returns plain data, so the
orchestrator reads top to bottom and phases can be exercised on their own.
"""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .deps import (engine_warnings, strip_refresh_defeating_args, version_at_least)
from .discovery import (discover_units, build_dag, generate_stacks, unit_path_filters,
                        MAX_UNIT_FILTERS)
from .hygiene import check_hygiene, attach_hygiene
from .incremental import load_manifest, hash_file, save_manifest
from .process import process_result
from .profiles import build_unit_profile_map, state_bucket_profile
from .report import generate_report
from .state_age import query_blob_dates
from .worker import run_plan, uses_provider_cache


def clean_cache(repo_root):
    """Clean terragrunt cache and terraform lock files."""
    print("\U0001f9f9 Cleaning .terragrunt-cache dirs and .terraform.lock.hcl files...")
    subprocess.run(
        'find . -type d -name ".terragrunt-cache" -prune -exec rm -rf {} + ; '
        'find . -type f -name ".terraform.lock.hcl" -delete',
        shell=True, cwd=str(repo_root),
    )


def setup(repo_root, timestamp):
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


def apply_incremental(args, units, output_dir):
    """Filter units for incremental/affected mode. Returns (filtered_units, last_json)."""
    last_json = None
    if not (args.incremental or args.affected):
        return units, last_json

    prev_jsons = sorted(output_dir.glob("terrahawk_*.json"), key=os.path.getmtime, reverse=True)
    if prev_jsons:
        last_json = prev_jsons[0]
        if args.incremental:
            prev_manifest_path = str(last_json).replace(".json", ".manifest")
            prev_manifest = load_manifest(prev_manifest_path)
            original_count = len(units)
            units = [(ud, rp) for ud, rp in units if hash_file(os.path.join(ud, "terragrunt.hcl")) != prev_manifest.get(rp, "")]
            skipped = original_count - len(units)
            print(f"  Incremental: {len(units)} changed, {skipped} reused from {last_json.name}")
        else:
            # --affected: units already filtered at discovery; previous report
            # only needed for merging the unaffected ones back in.
            print(f"  Affected: {len(units)} units to scan, rest merged from {last_json.name}")
    else:
        if args.incremental:
            print("  \u26a0\ufe0f  No previous report found, running full scan.")
            args.incremental = False
        else:
            print("  \u26a0\ufe0f  No previous report found \u2014 report will only contain affected units.")

    return units, last_json


def execute_plans(units, waves, args, unit_timeouts, repo_root, tmp_dir):
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
                    dur = result.get("duration")
                    dur_str = f" ({dur:.0f}s)" if dur is not None else ""
                    print(f"  {icon} {rp:65s} {label}{dur_str}")
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


def assemble_results(raw_results, args, blob_dates, root_provider_tpl, last_json):
    """Process raw results and merge incremental data."""
    print("\U0001f4ca Assembling results...")
    results = [
        process_result(r, args, blob_dates, root_provider_tpl)
        for r in raw_results
    ]

    # Incremental/affected merge
    if (args.incremental or args.affected) and last_json and last_json.exists():
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


def write_outputs(results, json_report, html_report, manifest_path, config_dir, report_date, versions, args, stack_graphs=None, meta=None):
    """Write JSON report, manifest, and HTML report."""
    # Write JSON
    with open(json_report, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Save manifest
    save_manifest(config_dir, str(manifest_path))

    # Generate HTML
    print("\U0001f4dd Generating HTML report...")
    generate_report(results, str(html_report), report_date, versions, args, stack_graphs, meta)


def print_summary(results, total_to_scan, html_report, json_report, args, trend=None):
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
    ignored = sum(1 for r in results if r.get("ignoredStatus"))
    if ignored:
        print(f"  \U0001f648 Ignored    : {ignored} unit(s) whose drift is all in .terrahawkignore")
    if trend and trend.get("baseline"):
        c = trend["counts"]
        print(f"  \u2728 vs {trend['baseline']}: {c['newDrift']} new drift, {c['newError']} new error(s), "
              f"{c['persisting']} persisting, {c['resolved']} resolved")

    print(f"""
  \U0001f4ca HTML report : {html_report}
  \U0001f4c4 JSON data   : {json_report}
""")


def cleanup(tmp_dir, repo_root, clean_stacks=False):
    """Clean up temporary files and restore git state."""
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Remove stack trees we materialised from terragrunt.stack.hcl (only when
    # this run generated them — never touch a user's pre-existing tree).
    if clean_stacks:
        subprocess.run(
            'find . -type d -name ".terragrunt-stack" -prune -exec rm -rf {} +',
            shell=True, cwd=str(repo_root), capture_output=True,
        )

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


def print_banner(args, config_dir, html_report, versions):
    """Startup banner, plus warnings that apply to the whole run."""
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
  Providers   : {'terragrunt provider cache' if uses_provider_cache(os.environ, args.tg_version, not args.no_provider_cache) else 'plugin cache dir'}
  HTML report : {html_report}
""")

    # A drift scan must refresh every resource. Strip flags that defeat that
    # from the inherited TF_CLI_ARGS* before any plan runs (children inherit).
    for var, flag in strip_refresh_defeating_args(os.environ):
        print(f"  \u26a0\ufe0f  Ignoring {flag} from {var}: it stops plan from refreshing every "
              "resource, which hides out-of-band drift.")
    for warning in engine_warnings(versions):
        print(f"  \u26a0\ufe0f  {warning}")


def materialise_stacks(args, config_dir):
    """Generate explicit stacks (terragrunt.stack.hcl) so their units are
    discoverable by `terragrunt find` and plannable by the worker. Auto-
    detected; disabled with --no-stacks. Returns True when this run generated
    stack trees (cleanup removes them afterwards)."""
    if args.no_stacks:
        return False
    n_stacks, n_failed = generate_stacks(config_dir, args.terragrunt_version)
    if not n_stacks:
        return False
    print(f"\U0001f4e6 Generated {n_stacks - n_failed}/{n_stacks} stack(s) from terragrunt.stack.hcl")
    return True


def discover(args, config_dir):
    """Discover units and apply --unit. Exits on discovery failure or when
    --unit matches nothing.

    Returns (units, native_deps, dir_by_relpath) where dir_by_relpath maps
    every discovered rel_path (before --unit / incremental filtering) to its
    unit dir, so per-stack diagrams can resolve DAG edges for merged units.
    """
    print("\U0001f4cb Discovering units...")
    filter_expr = f"[{args.affected}...HEAD]" if args.affected else None
    # --unit pre-narrowing (Terragrunt >= 1.2): pass the needle's candidate
    # unit paths as find --filter path expressions so terragrunt skips most
    # of the filesystem walk. Same match rule as the post-filter below, so
    # the selected units are identical — only discovery is faster. Skipped
    # under --affected (git + path filters would union, widening the set).
    unit_filters = None
    if args.unit and not filter_expr and version_at_least(args.tg_version, (1, 2, 0)):
        unit_filters = unit_path_filters(config_dir, args.unit)
        if unit_filters and len(unit_filters) > MAX_UNIT_FILTERS:
            print(f"  --unit: {len(unit_filters)} candidate paths (> {MAX_UNIT_FILTERS}), "
                  "skipping discovery narrowing")
            unit_filters = None
        elif unit_filters:
            print(f"  --unit: narrowing discovery to {len(unit_filters)} candidate path(s)")
    try:
        units, native_deps = discover_units(
            config_dir, args.exclude, args.terragrunt_version, filter_expr, unit_filters)
    except RuntimeError as e:
        print(f"  \u274c {e}")
        sys.exit(1)
    dir_by_relpath = {rp: ud for ud, rp in units}
    engine = "terragrunt find" if native_deps is not None else "glob fallback"
    print(f"  Found {len(units)} units (via {engine})")
    if args.affected:
        print(f"  Affected mode: units changed since '{args.affected}'")
    if args.exclude:
        print(f"  Exclude pattern: {args.exclude}")

    if args.unit:
        needle = args.unit.strip("/")
        units = [(ud, rp) for ud, rp in units if rp == needle or rp.endswith("/" + needle)]
        if not units:
            print(f"\n  \u274c No unit found matching '{args.unit}'")
            print("     Run without --unit to list all discovered units.")
            sys.exit(1)
        print(f"  Single-unit mode: {units[0][1]}")
    return units, native_deps, dir_by_relpath


def resolve_profiles(args, units, config_dir):
    """Multi-account: map each unit to the AWS profile owning its account so
    one scan spans several accounts into a single report. Sets
    args.unit_profiles (empty without --profile) and returns the profile to
    list the state bucket with (or None)."""
    args.unit_profiles = {}
    if not getattr(args, "profile", None):
        return None
    print(f"\U0001f511 Resolving AWS profiles: {', '.join(args.profile)}")
    args.unit_profiles, acct_prof = build_unit_profile_map(units, config_dir, args.profile)
    if len(args.profile) == 1:
        return args.profile[0]
    per_profile = Counter(args.unit_profiles.values())
    summary = ", ".join(f"{p}={per_profile[p]}" for p in args.profile if per_profile.get(p))
    print(f"  Units per profile: {summary or 'none matched'}")
    return state_bucket_profile(config_dir, acct_prof, args.profile)


def run_hygiene(args, config_dir):
    """HCL hygiene (terragrunt 1.x: hcl validate + hcl format --check)."""
    print("\U0001f9fc Checking HCL hygiene...")
    hygiene = check_hygiene(config_dir, args.terragrunt_version)
    if hygiene is None:
        print("  Skipped (requires Terragrunt 1.x)")
    else:
        n_diag = sum(len(v) for v in hygiene["diagnostics"].values())
        n_fmt = len(hygiene["unformatted"])
        print(f"  {n_diag} validation issue(s), {n_fmt} unformatted file(s)")
    return hygiene


def apply_hygiene(results, hygiene):
    """Annotate entries with hygiene findings; print root-level leftovers."""
    leftovers = attach_hygiene(results, hygiene)
    if not leftovers:
        return
    for d in leftovers["diagnostics"]:
        print(f"  \u26a0\ufe0f  HCL {d['severity']}: {d['file']}:{d.get('line', '?')} {d['summary']}")
    for f in leftovers["unformatted"]:
        print(f"  \u26a0\ufe0f  Needs formatting: {f}")


def query_state_ages(config_dir, state_profile):
    """One-shot state listing. State lives in one (possibly cross-account)
    bucket, so it runs under the bucket owner's profile when scanning
    multiple accounts; AWS_PROFILE is restored afterwards."""
    print("\U0001f4c5 Querying state file ages...")
    saved = os.environ.get("AWS_PROFILE")
    if state_profile:
        os.environ["AWS_PROFILE"] = state_profile
    try:
        return query_blob_dates(config_dir)
    finally:
        if state_profile:
            if saved is None:
                os.environ.pop("AWS_PROFILE", None)
            else:
                os.environ["AWS_PROFILE"] = saved


def build_waves(args, units, native_deps):
    """Topological execution waves, or None for flat parallelism."""
    if not (args.dag and units):
        return None
    print("\U0001f517 Building dependency DAG...")
    waves = build_dag(units, native_deps)
    # A single wave means no cross-unit dependency ordering exists; wave
    # scheduling would only add pointless serialization.
    if len(waves) <= 1:
        print("  \u2139\ufe0f  No cross-unit dependencies \u2014 using flat parallelism.")
        return None
    print(f"  {len(waves)} execution waves from {len(units)} units")
    return waves
