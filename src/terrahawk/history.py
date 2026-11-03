"""Run-over-run comparison: what changed since the previous report.

A daily scan's reader needs the delta, not the full list: which drift is
*new* today, which is *persisting* (and since when), and which got
*resolved*. `annotate()` compares the current entries with a baseline report
(by default the newest JSON in `terrahawk_results/`; in CI, point
`--baseline` at the previous run's JSON — a local path or an http(s) URL).

Per-entry fields added:
- `trend`: "new" | "persisting" | "resolved" | "" (no change / no baseline)
- `prevStatus`: the unit's status in the baseline ("" when absent)
- `since`: ISO date the current finding (drift/error/timeout) was first seen;
  carried forward across runs while it persists
- `planResources[].isNew` / `driftedResources[].isNew`: the resource change
  was not in the baseline (only set when a baseline exists)

Stdlib only.
"""

import json
import os
import re
import urllib.request
from datetime import datetime

FINDINGS = ("drift", "error", "timeout")

_TS_RE = re.compile(r"terrahawk_(\d{8})_(\d{6})")


def _is_partial(entries):
    """Reports from `--unit` runs only hold one unit: not a usable baseline."""
    return bool(entries) and bool(entries[0].get("partialRun"))


def _date_from_name(name):
    m = _TS_RE.search(name or "")
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def _load_json_list(text):
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("baseline is not a terrahawk JSON report (expected a list)")
    return data


def load_baseline(spec, output_dir, exclude=None):
    """Resolve the baseline report.

    spec: None (newest non-partial `terrahawk_*.json` in output_dir), a local
    path, or an http(s) URL. Returns (entries, label, date_iso) or
    (None, reason, "") when no baseline is available.
    """
    if spec:
        if re.match(r"^https?://", spec):
            try:
                with urllib.request.urlopen(spec, timeout=30) as resp:
                    entries = _load_json_list(resp.read().decode("utf-8"))
            except Exception as e:  # network, HTTP, JSON — report, never fail the scan
                return None, f"could not fetch baseline URL ({type(e).__name__}: {e})", ""
            label = spec.rsplit("/", 1)[-1] or spec
            return entries, label, _date_from_name(label)
        if not os.path.isfile(spec):
            return None, f"baseline file not found: {spec}", ""
        try:
            entries = _load_json_list(open(spec).read())
        except Exception as e:
            return None, f"could not read baseline {spec}: {e}", ""
        name = os.path.basename(spec)
        date_iso = _date_from_name(name) or datetime.fromtimestamp(
            os.path.getmtime(spec)).date().isoformat()
        return entries, name, date_iso

    if not output_dir or not os.path.isdir(output_dir):
        return None, "no previous report", ""
    candidates = sorted(
        (os.path.join(output_dir, f) for f in os.listdir(output_dir)
         if f.startswith("terrahawk_") and f.endswith(".json")),
        key=os.path.getmtime, reverse=True)
    for path in candidates:
        if exclude and os.path.abspath(path) == os.path.abspath(str(exclude)):
            continue
        try:
            entries = _load_json_list(open(path).read())
        except Exception:
            continue
        if _is_partial(entries):
            continue
        name = os.path.basename(path)
        return entries, name, _date_from_name(name)
    return None, "no previous report", ""


def _res_key(r, kind):
    return (kind, r.get("address"), r.get("action") or ",".join(r.get("actions") or []))


def annotate(results, baseline, today_iso, baseline_date=""):
    """Add trend fields to `results` in place. Returns summary counts.

    baseline: list of baseline entries, or None (no comparison: every
    trend is "" and `since` is today for current findings).
    """
    prev = {e.get("unit"): e for e in (baseline or [])}
    counts = {"new": 0, "persisting": 0, "resolved": 0,
              "newDrift": 0, "newError": 0}
    for e in results:
        cur = e.get("status")
        p = prev.get(e.get("unit")) if baseline is not None else None
        pst = p.get("status", "") if p else ""
        e["prevStatus"] = pst
        trend = ""
        since = ""
        if cur in FINDINGS:
            if baseline is None:
                since = today_iso
            elif p is not None and pst == cur:
                trend = "persisting"
                since = p.get("since") or baseline_date or ""
            else:
                trend = "new"
                since = today_iso
        elif cur == "clean" and pst in FINDINGS:
            trend = "resolved"
        e["trend"] = trend
        e["since"] = since
        if trend:
            counts[trend] += 1
        if trend == "new":
            counts["newDrift" if cur == "drift" else "newError"] += 1

        if baseline is None:
            continue
        prev_keys = set()
        if p:
            prev_keys |= {_res_key(r, "plan") for r in (p.get("planResources") or [])}
            prev_keys |= {_res_key(r, "oob") for r in (p.get("driftedResources") or [])}
        for r in (e.get("planResources") or []):
            r["isNew"] = _res_key(r, "plan") not in prev_keys
        for r in (e.get("driftedResources") or []):
            r["isNew"] = _res_key(r, "oob") not in prev_keys
    return counts
