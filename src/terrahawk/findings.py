"""Post-scan annotation of report entries and CI exit status.

After plans are processed, entries are enriched in this order — each step
reads what the previous one wrote:

1. owners (CODEOWNERS)
2. ignore rules (.terrahawkignore) — may turn a drift unit clean
3. `partialRun` marker on `--unit` reports (never a baseline)
4. run-over-run trend vs the baseline (uses the post-ignore status)
"""

import os
from datetime import date

from .history import annotate, load_baseline
from .ignore import apply_rules, load_rules
from .owners import attach_owners


def annotate_findings(results, args, repo_root, config_dir, output_dir, json_report, today=None):
    """Enrich entries in place. Returns the trend summary
    `{baseline, baselineDate, counts}` stored in the report's META."""
    today = today or date.today()

    co_path = attach_owners(results, repo_root, config_dir, getattr(args, "codeowners", None))
    if co_path:
        owned = sum(1 for r in results if r.get("owners"))
        print(f"\U0001f465 Owners: {owned}/{len(results)} units matched in "
              f"{os.path.relpath(co_path, str(repo_root))}")
    elif getattr(args, "codeowners", None):
        print(f"  ⚠️  CODEOWNERS file not found: {args.codeowners}")

    rules, rule_warnings, rules_path = load_rules(repo_root, getattr(args, "ignore_file", None))
    for w in rule_warnings:
        print(f"  ⚠️  Ignore rules: {w}")
    expired = apply_rules(results, rules, today)
    if rules_path:
        n_ign = sum(r.get("ignoredCount", 0) for r in results)
        print(f"\U0001f648 Ignore rules: {len(rules)} rule(s), {n_ign} change(s) ignored")
        for r in expired:
            print(f"  ⚠️  Ignore rule expired (line {r.line}, until {r.until}): {r.text}")
        # Unused rules only mean something on a full scan.
        if not (args.unit or args.incremental or args.affected):
            for r in rules:
                if r.hits == 0 and r not in expired:
                    print(f"  ℹ️  Ignore rule matched nothing (line {r.line}): {r.text}")

    if args.unit:
        for r in results:
            r["partialRun"] = True

    baseline, label, baseline_date = load_baseline(
        getattr(args, "baseline", None), str(output_dir), json_report)
    if baseline is None and getattr(args, "baseline", None):
        print(f"  ⚠️  Baseline: {label}")
    counts = annotate(results, baseline, today.isoformat(), baseline_date)
    return {"baseline": label if baseline is not None else "",
            "baselineDate": baseline_date, "counts": counts}


def exit_code(results, counts, fail_on):
    """CI gating: 2 signals findings (1 is reserved for terrahawk failures).

    - never: always 0
    - error: errors/timeouts
    - drift: any drift, error or timeout
    - new:   drift/errors that are new since the baseline
    Ignored drift is clean by then, so it never fails the run.
    """
    drift = sum(1 for r in results if r["status"] == "drift")
    errors = sum(1 for r in results if r["status"] in ("error", "timeout"))
    if fail_on == "new" and counts.get("new", 0) > 0:
        print(f"⚠️  --fail-on=new: exiting 2 ({counts.get('newDrift', 0)} new drift, "
              f"{counts.get('newError', 0)} new error(s))")
        return 2
    if (fail_on == "error" and errors) or (fail_on == "drift" and (drift or errors)):
        print(f"⚠️  --fail-on={fail_on}: exiting 2 (drift={drift}, error={errors})")
        return 2
    return 0
