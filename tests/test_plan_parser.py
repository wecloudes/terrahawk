"""Unit tests for terrahawk.plan_parser sensitive-value handling."""

import pytest

from terrahawk import plan_parser


class TestMaskSensitive:
    def test_scalar_marked_sensitive_is_masked(self):
        assert plan_parser._mask_sensitive("hunter2", True) == "(sensitive value)"

    def test_scalar_not_sensitive_passes_through(self):
        assert plan_parser._mask_sensitive("plain", False) == "plain"
        assert plan_parser._mask_sensitive(42, False) == 42

    def test_nested_dict_masks_only_flagged_keys(self):
        value = {"user": "alice", "password": "s3cr3t", "port": 5432}
        sensitive = {"password": True}
        out = plan_parser._mask_sensitive(value, sensitive)
        assert out == {
            "user": "alice",
            "password": "(sensitive value)",
            "port": 5432,
        }

    def test_nested_list_masks_by_index(self):
        value = ["a", "b", "c"]
        sensitive = [False, True, False]
        assert plan_parser._mask_sensitive(value, sensitive) == ["a", "(sensitive value)", "c"]

    def test_list_shorter_sensitive_defaults_false(self):
        value = ["a", "b", "c"]
        sensitive = [True]  # only first flagged
        assert plan_parser._mask_sensitive(value, sensitive) == ["(sensitive value)", "b", "c"]

    def test_deeply_nested_structure(self):
        value = {"outer": {"inner": ["x", "y"]}}
        sensitive = {"outer": {"inner": [False, True]}}
        assert plan_parser._mask_sensitive(value, sensitive) == {
            "outer": {"inner": ["x", "(sensitive value)"]}
        }

    def test_whole_dict_flagged_sensitive(self):
        value = {"a": 1, "b": 2}
        assert plan_parser._mask_sensitive(value, True) == "(sensitive value)"


class TestCountBraces:
    def test_balanced_and_net(self):
        assert plan_parser._count_braces("{ }") == 0
        assert plan_parser._count_braces("resource {") == 1
        assert plan_parser._count_braces("}") == -1

    def test_braces_in_strings_ignored(self):
        assert plan_parser._count_braces('x = "a{b}c"') == 0
        assert plan_parser._count_braces('x = "{" ') == 0

    def test_hash_comment_ignored(self):
        # a '#' comment with an unbalanced brace must not affect depth
        assert plan_parser._count_braces("foo = 1  # note }") == 0
        assert plan_parser._count_braces("# { { {") == 0

    def test_slash_comment_ignored(self):
        assert plan_parser._count_braces("foo = 1  // }}}") == 0


class TestParsePlanResourcesBraceWalk:
    def test_heredoc_body_braces_do_not_truncate_block(self):
        plan = "\n".join([
            "  # aws_instance.web will be updated in-place",
            '  ~ resource "aws_instance" "web" {',
            "      ~ user_data = <<-EOT",
            "            #!/bin/bash",
            "            echo { unbalanced brace",
            "        EOT",
            '      ~ ami       = "old" -> "new"',
            "    }",
        ])
        res = plan_parser.parse_plan_resources(plan)
        assert len(res) == 1
        r = res[0]
        assert r["address"] == "aws_instance.web"
        assert r["action"] == "update"
        # the block must capture through its real closing brace, not stop early
        assert 'ami       = "old" -> "new"' in r["body"]
        assert r["body"].rstrip().endswith("}")

    def test_inline_comment_brace_does_not_leak_block(self):
        plan = "\n".join([
            "  # aws_s3_bucket.b will be created",
            '  + resource "aws_s3_bucket" "b" {',
            '      + bucket = "x"  # trailing } brace in comment',
            "    }",
            "extra line that must not be swallowed",
        ])
        res = plan_parser.parse_plan_resources(plan)
        assert len(res) == 1
        assert "extra line that must not be swallowed" not in res[0]["body"]


class TestRedactByKeyname:
    """Guarded: helper may be added by parallel work."""

    def test_secret_named_keys_redacted(self):
        if not hasattr(plan_parser, "_redact_by_keyname"):
            pytest.skip("_redact_by_keyname not present yet")
        obj = {
            "password": "abc",
            "api_key": "xyz",
            "secret_token": "tok",
            "username": "bob",
            "region": "eu-west-1",
        }
        out = plan_parser._redact_by_keyname(obj)
        assert out["password"] == "(redacted)"
        assert out["api_key"] == "(redacted)"
        assert out["secret_token"] == "(redacted)"
        # normal keys pass through untouched
        assert out["username"] == "bob"
        assert out["region"] == "eu-west-1"

    def test_nested_and_non_string_values_untouched(self):
        if not hasattr(plan_parser, "_redact_by_keyname"):
            pytest.skip("_redact_by_keyname not present yet")
        obj = {"config": {"password": "p"}, "count": 3, "tokens": [1, 2]}
        out = plan_parser._redact_by_keyname(obj)
        assert out["config"]["password"] == "(redacted)"
        # non-string value under a secret-ish key is left alone (no structural change)
        assert out["count"] == 3
        assert out["tokens"] == [1, 2]


def _b64gz(text, **kw):
    import base64
    import gzip
    return base64.b64encode(gzip.compress(text.encode(), **kw)).decode()


class TestEncodingOnly:
    """gzip re-encodings (e.g. Terragrunt 1.2's base64gzip encoder change)."""

    SCRIPT = "#!/bin/bash\necho hello\n" * 20

    def test_same_content_different_bytes(self):
        before, after = _b64gz(self.SCRIPT, mtime=0), _b64gz(self.SCRIPT, mtime=1)
        assert before != after
        change = {"before": {"user_data_base64": before, "ami": "ami-1"},
                  "after": {"user_data_base64": after, "ami": "ami-1"}}
        assert plan_parser.encoding_only_paths(change) == ["user_data_base64"]

    def test_different_content_is_real_change(self):
        change = {"before": {"user_data_base64": _b64gz(self.SCRIPT)},
                  "after": {"user_data_base64": _b64gz(self.SCRIPT + "echo bye\n")}}
        assert plan_parser.encoding_only_paths(change) == []

    def test_any_other_known_change_disqualifies(self):
        change = {"before": {"user_data_base64": _b64gz(self.SCRIPT, mtime=0), "ami": "ami-1"},
                  "after": {"user_data_base64": _b64gz(self.SCRIPT, mtime=1), "ami": "ami-2"}}
        assert plan_parser.encoding_only_paths(change) == []

    def test_computed_unknowns_are_ignored(self):
        # A replacement makes id/arn unknown; they are consequences, not causes.
        change = {"before": {"id": "i-123", "arn": "arn:x", "user_data_base64": _b64gz(self.SCRIPT, mtime=0)},
                  "after": {"user_data_base64": _b64gz(self.SCRIPT, mtime=1)},
                  "after_unknown": {"id": True, "arn": True}}
        assert plan_parser.encoding_only_paths(change) == ["user_data_base64"]

    def test_nested_paths(self):
        change = {"before": {"launch_template": [{"user_data": _b64gz(self.SCRIPT, mtime=0)}]},
                  "after": {"launch_template": [{"user_data": _b64gz(self.SCRIPT, mtime=1)}]}}
        assert plan_parser.encoding_only_paths(change) == ["launch_template.0.user_data"]

    def test_plain_base64_and_non_strings_not_flagged(self):
        import base64
        plain = {"before": {"v": base64.b64encode(b"a" * 40).decode()},
                 "after": {"v": base64.b64encode(b"b" * 40).decode()}}
        assert plan_parser.encoding_only_paths(plain) == []
        assert plan_parser.encoding_only_paths({"before": {"n": 1}, "after": {"n": 2}}) == []
        assert plan_parser.encoding_only_paths({"before": None, "after": {"a": 1}}) == []
        assert plan_parser.encoding_only_paths({"before": {"a": 1}, "after": {"a": 1}}) == []

    def test_parse_json_annotates_replace_not_create(self):
        plan = {"resource_changes": [
            {"address": "aws_instance.web", "type": "aws_instance",
             "change": {"actions": ["delete", "create"],
                        "before": {"user_data_base64": _b64gz(self.SCRIPT, mtime=0)},
                        "after": {"user_data_base64": _b64gz(self.SCRIPT, mtime=1)}}},
            {"address": "aws_s3_bucket.b", "type": "aws_s3_bucket",
             "change": {"actions": ["create"], "before": None, "after": {"bucket": "b"}}},
        ]}
        res = {r["address"]: r for r in plan_parser.parse_plan_resources_json(plan)}
        assert res["aws_instance.web"]["action"] == "replace"
        assert res["aws_instance.web"]["encodingOnly"] == ["user_data_base64"]
        assert "encodingOnly" not in res["aws_s3_bucket.b"]
