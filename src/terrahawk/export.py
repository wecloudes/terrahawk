"""CI-native outputs: SARIF 2.1.0 and JUnit XML.

- SARIF (`--sarif PATH`): one result per drifted resource / out-of-band
  change / failed unit, located at the unit's `terragrunt.hcl`. GitHub code
  scanning (`github/codeql-action/upload-sarif`) and other SARIF viewers turn
  these into annotations; stable `partialFingerprints` let them track an
  alert across daily runs.
- JUnit (`--junit PATH`): one test case per unit — drift is a `<failure>`,
  error/timeout an `<error>`. GitLab (`artifacts:reports:junit`), Azure
  DevOps (`PublishTestResults@2`) and Jenkins render it natively.

Ignored changes (`.terrahawkignore`) are left out of SARIF and reported as
passing (with a note) in JUnit. Plan values are never emitted. Stdlib only.
"""

import hashlib
import json
import os
import xml.etree.ElementTree as ET

from .owners import unit_repo_path

_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

_RULES = [
    {"id": "TH001", "name": "PlannedDrift",
     "shortDescription": {"text": "Infrastructure drift: plan has pending changes"},
     "fullDescription": {"text": "terragrunt plan wants to change this resource: the live "
                                 "infrastructure no longer matches the code."},
     "defaultConfiguration": {"level": "warning"}},
    {"id": "TH002", "name": "ChangedOutsideTerraform",
     "shortDescription": {"text": "Resource changed outside Terraform"},
     "fullDescription": {"text": "Refresh found changes made outside Terraform "
                                 "(out-of-band drift)."},
     "defaultConfiguration": {"level": "warning"}},
    {"id": "TH003", "name": "UnitPlanError",
     "shortDescription": {"text": "Unit failed to plan"},
     "fullDescription": {"text": "terragrunt plan failed, so drift for this unit is unknown."},
     "defaultConfiguration": {"level": "error"}},
    {"id": "TH004", "name": "UnitPlanTimeout",
     "shortDescription": {"text": "Unit plan timed out"},
     "fullDescription": {"text": "terragrunt plan exceeded its timeout, so drift for this "
                                 "unit is unknown."},
     "defaultConfiguration": {"level": "error"}},
]


def _fingerprint(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _location_path(entry, repo_root, config_dir):
    """Repo-relative file to anchor results on. Generated stack units point
    at their stack's terragrunt.stack.hcl (the generated tree isn't in git)."""
    unit = entry["unit"]
    if "/.terragrunt-stack/" in "/" + unit:
        before = unit.partition("/.terragrunt-stack/")[0]
        rel = os.path.relpath(os.path.join(str(config_dir), before, "terragrunt.stack.hcl"),
                              str(repo_root))
        return rel.replace(os.sep, "/")
    return unit_repo_path(repo_root, config_dir, unit)


def _first_line(text, limit=300):
    for line in (text or "").splitlines():
        line = line.strip(" │╷╵\t")
        if line:
            return line[:limit]
    return ""


def build_sarif(results, repo_root, config_dir, version):
    sarif_results = []
    for e in results:
        loc = [{"physicalLocation": {
            "artifactLocation": {"uri": _location_path(e, repo_root, config_dir),
                                 "uriBaseId": "%SRCROOT%"},
            "region": {"startLine": 1}}}]
        props = {"unit": e["unit"], "owners": e.get("owners") or [],
                 "trend": e.get("trend", "")}
        status = e.get("status")
        if status == "drift":
            for r in e.get("planResources") or []:
                if r.get("ignored"):
                    continue
                attrs = r.get("changedAttrs") or []
                msg = f"{r.get('address')}: {r.get('action')} planned in {e['unit']}"
                if attrs:
                    msg += f" (changed: {', '.join(attrs[:10])}{', …' if len(attrs) > 10 else ''})"
                sarif_results.append({
                    "ruleId": "TH001", "level": "warning",
                    "message": {"text": msg}, "locations": loc,
                    "partialFingerprints": {"terrahawk/v1": _fingerprint(
                        e["unit"], r.get("address"), r.get("action"))},
                    "properties": dict(props, address=r.get("address"),
                                       action=r.get("action"), isNew=bool(r.get("isNew"))),
                })
        elif status in ("error", "timeout"):
            rule = "TH003" if status == "error" else "TH004"
            detail = _first_line(e.get("error")) or status
            sarif_results.append({
                "ruleId": rule, "level": "error",
                "message": {"text": f"{e['unit']}: {detail}"}, "locations": loc,
                "partialFingerprints": {"terrahawk/v1": _fingerprint(e["unit"], rule)},
                "properties": dict(props, errorClass=e.get("errorClass", "")),
            })
        for r in e.get("driftedResources") or []:
            if r.get("ignored"):
                continue
            sarif_results.append({
                "ruleId": "TH002", "level": "warning",
                "message": {"text": f"{r.get('address')} changed outside Terraform in {e['unit']}"},
                "locations": loc,
                "partialFingerprints": {"terrahawk/v1": _fingerprint(
                    e["unit"], "oob", r.get("address"))},
                "properties": dict(props, address=r.get("address"),
                                   isNew=bool(r.get("isNew"))),
            })
    return {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "terrahawk", "version": version,
                                "semanticVersion": version, "rules": _RULES}},
            "automationDetails": {"id": "terrahawk/drift/"},
            "results": sarif_results,
        }],
    }


def write_sarif(results, path, repo_root, config_dir, version):
    doc = build_sarif(results, repo_root, config_dir, version)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return len(doc["runs"][0]["results"])


def build_junit(results):
    suites = {}
    for e in results:
        group = "/".join(p for p in (e.get("environment"), e.get("subscription")) if p) or "units"
        suites.setdefault(group, []).append(e)

    root = ET.Element("testsuites", name="terrahawk")
    tot = {"tests": 0, "failures": 0, "errors": 0, "time": 0.0}
    for group in sorted(suites):
        entries = suites[group]
        suite = ET.SubElement(root, "testsuite", name=group)
        s = {"tests": 0, "failures": 0, "errors": 0, "time": 0.0}
        for e in entries:
            dur = float(e.get("duration") or 0)
            tc = ET.SubElement(suite, "testcase",
                               classname=f"terrahawk.{group.replace('/', '.')}",
                               name=e.get("displayUnit") or e["unit"], time=f"{dur:.1f}")
            s["tests"] += 1
            s["time"] += dur
            status = e.get("status")
            owners = ", ".join(e.get("owners") or [])
            if status == "drift":
                s["failures"] += 1
                live = [r for r in (e.get("planResources") or []) if not r.get("ignored")]
                body = "\n".join(f"{r.get('action'):8s} {r.get('address')}" for r in live)
                f = ET.SubElement(tc, "failure", type="drift",
                                  message=e.get("summary") or f"{len(live)} planned change(s)")
                f.text = body + (f"\nowners: {owners}" if owners else "")
            elif status in ("error", "timeout"):
                s["errors"] += 1
                err = ET.SubElement(tc, "error", type=e.get("errorClass") or status,
                                    message=_first_line(e.get("error")) or status)
                err.text = (e.get("error") or "")[:4000]
            notes = []
            if e.get("ignoredCount"):
                notes.append(f"{e['ignoredCount']} change(s) ignored by .terrahawkignore")
            oob = [r for r in (e.get("driftedResources") or []) if not r.get("ignored")]
            if oob:
                notes.append("changed outside Terraform: " + ", ".join(r.get("address", "?") for r in oob))
            if notes:
                ET.SubElement(tc, "system-out").text = "\n".join(notes)
        for k in ("tests", "failures", "errors"):
            suite.set(k, str(s[k]))
            tot[k] += s[k]
        suite.set("time", f"{s['time']:.1f}")
        tot["time"] += s["time"]
    for k in ("tests", "failures", "errors"):
        root.set(k, str(tot[k]))
    root.set("time", f"{tot['time']:.1f}")
    return root


def write_junit(results, path):
    root = build_junit(results)
    tree = ET.ElementTree(root)
    if hasattr(ET, "indent"):  # 3.9+
        ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return int(root.get("tests"))
