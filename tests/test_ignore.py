"""Known-drift ignore rules (.terrahawkignore)."""

from datetime import date

from terrahawk import ignore


def _entry(unit="prod/app", status="drift", res=None, oob=None):
    return {"unit": unit, "displayUnit": unit, "status": status,
            "planResources": res or [], "driftedResources": oob or []}


def _res(address, action="update", attrs=None):
    r = {"address": address, "type": address.split(".")[0], "action": action}
    if attrs is not None:
        r["changedAttrs"] = attrs
    return r


class TestParse:
    def test_fields_reason_and_until(self):
        rules, warns = ignore.parse_rules(
            "# comment\n"
            "*  *  tags,tags_all   # tagging service\n"
            "shared/vpc aws_sg.legacy until=2026-12-31\n")
        assert not warns
        assert rules[0].attrs == ["tags", "tags_all"] and rules[0].reason == "tagging service"
        assert rules[1].until == date(2026, 12, 31) and rules[1].attrs == []

    def test_bad_lines_warn(self):
        rules, warns = ignore.parse_rules("onlyone\nprod x until=31-12-2026\na b c d\n")
        assert rules == []
        assert len(warns) == 3

    def test_missing_default_file_is_silent(self, tmp_path):
        assert ignore.load_rules(tmp_path) == ([], [], None)

    def test_missing_explicit_file_warns(self, tmp_path):
        rules, warns, path = ignore.load_rules(tmp_path, "nope.ignore")
        assert rules == [] and path is None and "not found" in warns[0]


class TestGlob:
    def test_brackets_are_literal(self):
        rules, _ = ignore.parse_rules('* aws_instance.web["a"]\n')
        assert rules[0].matches_resource('aws_instance.web["a"]', "update", None)
        assert not rules[0].matches_resource('aws_instance.web["b"]', "update", None)

    def test_star_crosses_separators(self):
        rules, _ = ignore.parse_rules("prod/* module.*.aws_s3_bucket.*\n")
        e = _entry("prod/eu/app")
        assert rules[0].matches_unit(e)
        assert rules[0].matches_resource("module.a.module.b.aws_s3_bucket.x", "update", None)


class TestApply:
    def test_attr_rule_needs_every_change_covered(self):
        rules, _ = ignore.parse_rules("* * tags,tags_all\n")
        only_tags = _res("aws_s3_bucket.a", attrs=["tags.Owner", "tags_all.Owner"])
        mixed = _res("aws_s3_bucket.b", attrs=["tags.Owner", "versioning.0.enabled"])
        e = _entry(res=[only_tags, mixed])
        ignore.apply_rules([e], rules)
        assert only_tags.get("ignored") and not mixed.get("ignored")
        assert e["status"] == "drift" and e["ignoredCount"] == 1

    def test_attr_rule_never_ignores_create_or_unknown_attrs(self):
        rules, _ = ignore.parse_rules("* * tags\n")
        create = _res("aws_s3_bucket.a", action="create")
        no_attrs = _res("aws_s3_bucket.b", attrs=[])
        e = _entry(res=[create, no_attrs])
        ignore.apply_rules([e], rules)
        assert not create.get("ignored") and not no_attrs.get("ignored")

    def test_fully_ignored_unit_becomes_clean_and_is_restorable(self):
        rules, _ = ignore.parse_rules("prod/* aws_asg.* desired_capacity  # autoscaling\n")
        e = _entry(res=[_res("aws_asg.web", attrs=["desired_capacity"])])
        ignore.apply_rules([e], rules)
        assert e["status"] == "clean" and e["ignoredStatus"] == "drift"
        assert e["planResources"][0]["ignored"] == "autoscaling"
        # Re-applying with no rules restores the original state (idempotent).
        ignore.apply_rules([e], [])
        assert e["status"] == "drift" and "ignoredStatus" not in e
        assert "ignored" not in e["planResources"][0] and "ignoredCount" not in e

    def test_expired_rules_are_not_applied(self):
        rules, _ = ignore.parse_rules("* * until=2026-01-01\n")
        e = _entry(res=[_res("aws_x.a")])
        expired = ignore.apply_rules([e], rules, today=date(2026, 1, 2))
        assert expired == rules and e["status"] == "drift"
        # inclusive on the until date itself
        ignore.apply_rules([e], rules, today=date(2026, 1, 1))
        assert e["status"] == "clean"

    def test_out_of_band_drift_and_hits(self):
        rules, _ = ignore.parse_rules("* aws_sg.* ingress\n* nothing.matches\n")
        oob = {"address": "aws_sg.a", "actions": ["update"], "changedAttrs": ["ingress.0.cidr"]}
        e = _entry(status="clean", oob=[oob])
        ignore.apply_rules([e], rules)
        assert oob["ignored"] and e["ignoredCount"] == 1 and e["status"] == "clean"
        assert rules[0].hits == 1 and rules[1].hits == 0

    def test_unit_rule_matches_display_path(self):
        rules, _ = ignore.parse_rules("stk/services/api *\n")
        e = _entry("stk/.terragrunt-stack/services/api", res=[_res("aws_x.a")])
        e["displayUnit"] = "stk/services/api"
        ignore.apply_rules([e], rules)
        assert e["status"] == "clean"
