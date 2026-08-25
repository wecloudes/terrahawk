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
