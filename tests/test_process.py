"""Unit tests for terrahawk.process error taxonomy."""

import pytest

from terrahawk import process


class TestStackDisplayName:
    @pytest.mark.parametrize("before,expected", [
        ("dum/production/eu-west-1", "dum/production"),
        ("eysa/production/eu-west-1", "eysa/production"),
        ("eysa/preproduction/eu-west-1", "eysa/preproduction"),
        ("shared/eu-west-1", "shared"),
        ("prod/us-gov-west-1", "prod"),        # aws gov region
        ("prod/europe-west1", "prod"),         # gcp region
        ("envA/mystack", "envA/mystack"),      # non-region tail kept
        ("solo", "solo"),                      # single segment untouched
        ("eu-west-1", "eu-west-1"),            # lone region: nothing to strip
    ])
    def test_names(self, before, expected):
        assert process._stack_display_name(before) == expected


@pytest.mark.skipif(
    not hasattr(process, "_classify_error"),
    reason="_classify_error not present yet",
)
class TestClassifyError:
    @pytest.mark.parametrize(
        "status,error_text,expected",
        [
            # timeout: by status
            ("timeout", "Plan timed out.", "timeout"),
            # timeout: by text even when status is error
            ("error", "operation timed out after 300s", "timeout"),
            # auth
            ("error", "Error: Unable to locate credentials", "auth"),
            ("error", "AccessDenied: not authorized", "auth"),
            # init
            ("error", "Required plugins are not installed", "init"),
            ("error", "Failed to download module foo", "init"),
            # dependency
            ("error", "dependency.vpc has no output named id", "dependency"),
            # config
            ("error", "Unknown variable: region", "config"),
            ("error", "Reference to undeclared resource", "config"),
            # fallthrough plan error
            ("error", "some totally unrecognised failure", "plan"),
        ],
    )
    def test_taxonomy_mapping(self, status, error_text, expected):
        assert process._classify_error(status, error_text) == expected

    def test_non_error_status_returns_empty(self):
        assert process._classify_error("clean", "") == ""
        assert process._classify_error("drift", "Plan: 1 to add") == ""

    def test_case_insensitive(self):
        assert process._classify_error("error", "UNKNOWN VARIABLE foo") == "config"


class TestBuildPlanResources:
    _PLAN_JSON = {
        "resource_changes": [
            {"address": "aws_instance.web", "type": "aws_instance",
             "change": {"actions": ["create"], "before": None, "after": {"ami": "abc"}}},
            {"address": "aws_db_instance.pg", "type": "aws_db_instance",
             "change": {"actions": ["delete", "create"], "before": {"x": 1}, "after": {"x": 2}}},
            {"address": "aws_s3_bucket.noop", "type": "aws_s3_bucket",
             "change": {"actions": ["no-op"]}},
        ]
    }

    def test_json_is_authoritative_when_present(self):
        # Text parse finds nothing, JSON drives the change set (no-op filtered).
        res = process._build_plan_resources("drift", "", self._PLAN_JSON)
        got = {r["address"]: r["action"] for r in res}
        assert got == {"aws_instance.web": "create", "aws_db_instance.pg": "replace"}

    def test_text_body_grafted_onto_json(self):
        plan_text = (
            "  # aws_instance.web will be created\n"
            '  + resource "aws_instance" "web" {\n'
            '      + ami = "abc"\n'
            "    }\n"
        )
        res = process._build_plan_resources("drift", plan_text, self._PLAN_JSON)
        web = next(r for r in res if r["address"] == "aws_instance.web")
        # Human-readable text body wins over the JSON before/after dump.
        assert '+ resource "aws_instance" "web"' in web["body"]

    def test_falls_back_to_text_without_json(self):
        plan_text = (
            "  # aws_instance.web will be created\n"
            '  + resource "aws_instance" "web" {\n'
            "    }\n"
        )
        res = process._build_plan_resources("drift", plan_text, None)
        assert [r["address"] for r in res] == ["aws_instance.web"]

    def test_non_drift_without_json_is_empty(self):
        assert process._build_plan_resources("clean", "irrelevant", None) == []


class TestPlanSummary:
    def test_prefers_text_summary(self):
        assert process._plan_summary("Plan: 3 to add.", [{"action": "create"}]) == "Plan: 3 to add."

    def test_synthesizes_from_counts_when_text_missing(self):
        res = [{"action": "create"}, {"action": "replace"}, {"action": "update"}]
        # replace counts as both add and destroy
        assert process._plan_summary("", res) == "Plan: 2 to add, 1 to change, 1 to destroy."

    def test_empty_when_nothing(self):
        assert process._plan_summary("", []) == ""
        assert process._plan_summary("", [{"action": "read"}]) == ""
