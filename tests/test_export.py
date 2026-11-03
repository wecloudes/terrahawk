"""SARIF and JUnit outputs."""

import json
import xml.etree.ElementTree as ET

from terrahawk import export


def _results():
    return [
        {"unit": "prod/app", "displayUnit": "prod/app", "environment": "prod",
         "subscription": "app", "status": "drift", "summary": "Plan: 0 to add, 1 to change, 0 to destroy.",
         "duration": 12.0, "owners": ["@org/prod"], "trend": "new",
         "planResources": [
             {"address": "aws_s3_bucket.a", "action": "update", "changedAttrs": ["versioning.0.enabled"],
              "isNew": True},
             {"address": "aws_s3_bucket.b", "action": "update", "ignored": "tags"},
         ],
         "driftedResources": [{"address": "aws_sg.x", "actions": ["update"]}]},
        {"unit": "prod/db", "displayUnit": "prod/db", "environment": "prod", "subscription": "db",
         "status": "error", "errorClass": "auth", "error": "╷\n│ Error: AccessDenied\n│ more",
         "planResources": [], "driftedResources": []},
        {"unit": "stk/.terragrunt-stack/svc", "displayUnit": "stk/svc", "environment": "stk",
         "subscription": "svc", "status": "clean", "ignoredCount": 2, "ignoredStatus": "drift",
         "planResources": [], "driftedResources": []},
    ]


class TestSarif:
    def test_results_rules_and_locations(self, tmp_path):
        cfg = tmp_path / "live"
        doc = export.build_sarif(_results(), tmp_path, cfg, "9.9.9")
        run = doc["runs"][0]
        assert doc["version"] == "2.1.0" and run["tool"]["driver"]["version"] == "9.9.9"
        by_rule = {}
        for r in run["results"]:
            by_rule.setdefault(r["ruleId"], []).append(r)
        # ignored resource excluded; one drift, one oob, one error
        assert len(by_rule["TH001"]) == 1 and len(by_rule["TH002"]) == 1 and len(by_rule["TH003"]) == 1
        drift = by_rule["TH001"][0]
        assert drift["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "live/prod/app/terragrunt.hcl"
        assert "versioning.0.enabled" in drift["message"]["text"]
        assert drift["properties"]["isNew"] is True and drift["properties"]["owners"] == ["@org/prod"]
        assert by_rule["TH003"][0]["message"]["text"] == "prod/db: Error: AccessDenied"
        assert by_rule["TH003"][0]["level"] == "error"

    def test_fingerprints_are_stable(self, tmp_path):
        a = export.build_sarif(_results(), tmp_path, tmp_path, "1")
        b = export.build_sarif(_results(), tmp_path, tmp_path, "2")
        fa = [r["partialFingerprints"] for r in a["runs"][0]["results"]]
        fb = [r["partialFingerprints"] for r in b["runs"][0]["results"]]
        assert fa == fb and len({json.dumps(f) for f in fa}) == len(fa)

    def test_stack_units_point_at_stack_file(self, tmp_path):
        r = _results()[2]
        r["status"] = "error"
        doc = export.build_sarif([r], tmp_path, tmp_path, "1")
        uri = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        assert uri == "stk/terragrunt.stack.hcl"

    def test_write(self, tmp_path):
        out = tmp_path / "r.sarif"
        assert export.write_sarif(_results(), str(out), tmp_path, tmp_path, "1") == 3
        assert json.loads(out.read_text())["runs"][0]["results"]


class TestJunit:
    def test_cases_failures_errors(self, tmp_path):
        out = tmp_path / "j.xml"
        assert export.write_junit(_results(), str(out)) == 3
        root = ET.parse(str(out)).getroot()
        assert root.tag == "testsuites"
        assert (root.get("tests"), root.get("failures"), root.get("errors")) == ("3", "1", "1")
        cases = {c.get("name"): c for c in root.iter("testcase")}
        fail = cases["prod/app"].find("failure")
        assert fail is not None and "aws_s3_bucket.a" in fail.text
        assert "aws_s3_bucket.b" not in fail.text            # ignored
        assert "@org/prod" in fail.text
        assert cases["prod/db"].find("error").get("type") == "auth"
        clean = cases["stk/svc"]
        assert clean.find("failure") is None and "ignored" in clean.find("system-out").text
