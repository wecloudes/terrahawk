"""Unit tests for terrahawk.process error taxonomy."""

import pytest

from terrahawk import process


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
