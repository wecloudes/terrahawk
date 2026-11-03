"""End-to-end `cli.scan()` with terragrunt/terraform mocked out.

Exercises the orchestration across phases: discovery → plans → processing
→ owners / ignore rules / run-over-run trend → JSON + HTML + SARIF + JUnit
→ alert → exit code. Two runs: the second compares against the first.
"""

import json
import subprocess
import sys

import pytest

from terrahawk import cli, notify, phases


def _plan_json(address, attr, before, after):
    return {"resource_changes": [{
        "address": address, "type": address.split(".")[0],
        "change": {"actions": ["update"], "before": {attr: before}, "after": {attr: after}},
    }]}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    live = tmp_path / "terragrunt"  # standard layout: detect_config_dir step 2
    for rel in ("prod/app", "prod/db", "dev/web"):
        (live / rel).mkdir(parents=True)
        (live / rel / "terragrunt.hcl").write_text("")
    (live / "root.hcl").write_text("")
    (tmp_path / "CODEOWNERS").write_text("/terragrunt/prod/ @org/prod\n")
    (tmp_path / ".terrahawkignore").write_text("dev/* aws_instance.* tags  # tagging service\n")

    units = [(str(live / rel), rel) for rel in ("dev/web", "prod/app", "prod/db")]
    plans = {}  # rel -> raw result fields, set per run

    def fake_run_plan(ud, rp, timeout, args, unit_timeouts, repo_root, tmp_dir, idx):
        return dict({"unit": rp, "idx": idx, "duration": 1.0, "plan_output": "",
                     "throttle_retries": 0}, **plans[rp])

    monkeypatch.setattr(cli, "check_dependencies", lambda args: None)
    monkeypatch.setattr(cli, "get_versions", lambda args: {"terragrunt": "1.2.1"})
    monkeypatch.setattr(phases, "generate_stacks", lambda *a: (0, 0))
    monkeypatch.setattr(phases, "discover_units", lambda *a, **k: (list(units), {}))
    monkeypatch.setattr(phases, "check_hygiene", lambda *a: None)
    monkeypatch.setattr(phases, "query_blob_dates", lambda *a: {})
    monkeypatch.setattr(phases, "run_plan", fake_run_plan)
    # clean_cache / cleanup shell out to find, git and `open` — keep hermetic.
    monkeypatch.setattr(phases.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, payload: sent.append(payload) or 200)
    monkeypatch.setenv(notify.ENV_URL, "https://hooks.slack.com/services/T/B/X")
    return tmp_path, plans, sent


def _scan(monkeypatch, root, *extra):
    monkeypatch.setattr(sys, "argv", ["terrahawk", "-r", str(root), "--no-dag", *extra])
    return cli.scan()


def _latest(root):
    out = root / "terrahawk_results"
    return json.loads(sorted(out.glob("terrahawk_*.json"))[-1].read_text())


def test_two_runs_trend_ignore_owners_exports_alert(repo, monkeypatch):
    root, plans, sent = repo

    # Run 1: dev/web drift is tags-only (ignored), prod/app drift, prod/db throttled error.
    plans.update({
        "dev/web": {"exit_code": 2, "plan_json": _plan_json("aws_instance.w", "tags", {"a": "1"}, {"a": "2"})},
        "prod/app": {"exit_code": 2, "plan_json": _plan_json("aws_s3_bucket.a", "acl", "private", "public")},
        "prod/db": {"exit_code": 1, "plan_output": "Error: ThrottlingException: Rate exceeded",
                    "throttle_retries": 2},
    })
    assert _scan(monkeypatch, root, "--fail-on", "new") == 0      # no baseline yet: nothing "new"
    first = {e["unit"]: e for e in _latest(root)}
    assert first["dev/web"]["status"] == "clean" and first["dev/web"]["ignoredStatus"] == "drift"
    assert first["prod/app"]["owners"] == ["@org/prod"] and first["dev/web"]["owners"] == []
    assert first["prod/db"]["errorClass"] == "throttled" and first["prod/db"]["throttleRetries"] == 2
    assert sent == []                                              # notify_on=new, nothing new

    # Run 2: prod/app persists, prod/db recovers to drift (new), dev/web gains a real change.
    import time
    time.sleep(1.1)  # distinct report timestamp
    plans["prod/db"] = {"exit_code": 2, "plan_json": _plan_json("aws_db_instance.d", "size", 1, 2)}
    plans["dev/web"] = {"exit_code": 2, "plan_json": _plan_json("aws_instance.w", "ami", "a", "b")}
    sarif = root / "out.sarif"
    junit = root / "out.xml"
    rc = _scan(monkeypatch, root, "--fail-on", "new", "--sarif", str(sarif), "--junit", str(junit))
    assert rc == 2                                                 # new drift gates the run
    second = {e["unit"]: e for e in _latest(root)}
    assert second["prod/app"]["trend"] == "persisting"
    assert second["prod/db"]["trend"] == "new" and second["prod/db"]["prevStatus"] == "error"
    assert second["dev/web"]["trend"] == "new" and second["dev/web"]["status"] == "drift"

    data_js = sorted((root / "terrahawk_results").glob("*_data.js"))[-1].read_text()
    assert "window.TERRAHAWK_META=" in data_js and '"newDrift": 2' in data_js

    assert len(json.loads(sarif.read_text())["runs"][0]["results"]) == 3
    assert 'failures="3"' in junit.read_text()
    assert len(sent) == 1 and "prod/db" in json.dumps(sent[0])


def test_unit_run_is_partial_and_not_a_baseline(repo, monkeypatch):
    root, plans, _ = repo
    plans.update({rel: {"exit_code": 0} for rel in ("dev/web", "prod/app", "prod/db")})
    assert _scan(monkeypatch, root) == 0
    import time
    time.sleep(1.1)
    assert _scan(monkeypatch, root, "--unit", "prod/app") == 0
    assert all(e.get("partialRun") for e in _latest(root))
    time.sleep(1.1)
    plans["prod/db"] = {"exit_code": 2, "plan_json": _plan_json("aws_db_instance.d", "size", 1, 2)}
    _scan(monkeypatch, root)
    by_unit = {e["unit"]: e for e in _latest(root)}
    # compared with the full run 1, not the single-unit run 2
    assert by_unit["prod/db"]["trend"] == "new" and by_unit["dev/web"]["trend"] == ""


def test_webhook_in_config_is_ignored_and_warned(repo, monkeypatch, capsys):
    root, plans, sent = repo
    plans.update({rel: {"exit_code": 0} for rel in ("dev/web", "prod/app", "prod/db")})
    (root / ".terrahawk.yml").write_text("webhook_url: https://hooks.slack.com/services/LEAK\n")
    _scan(monkeypatch, root)
    out = capsys.readouterr().out
    assert "webhook URLs are secrets" in out and "LEAK" not in out


def test_exit_codes():
    from terrahawk.findings import exit_code
    res = [{"status": "drift"}, {"status": "timeout"}]
    assert exit_code(res, {"new": 0}, "never") == 0
    assert exit_code(res, {"new": 0}, "error") == 2
    assert exit_code([{"status": "drift"}], {"new": 0}, "error") == 0
    assert exit_code(res, {"new": 0}, "drift") == 2
    assert exit_code(res, {"new": 0}, "new") == 0
    assert exit_code(res, {"new": 1, "newDrift": 1}, "new") == 2
