"""Unit tests for terrahawk.deps: version parsing, mise wrapping, and
OpenTofu/Terraform binary resolution."""

import subprocess
from types import SimpleNamespace

import pytest

from terrahawk import deps


class TestParseVersion:
    @pytest.mark.parametrize("text,expected", [
        ("v1.1.6", (1, 1, 6)),
        ("terragrunt version v1.2.0-rc3", (1, 2, 0)),
        ("1.16.6", (1, 16, 6)),
        ("Terraform v1.17.0-rc2", (1, 17, 0)),
        ("n/a", None),
        ("", None),
        (None, None),
    ])
    def test_parse(self, text, expected):
        assert deps.parse_version(text) == expected

    def test_version_at_least(self):
        assert deps.version_at_least((1, 2, 0), (1, 2, 0))
        assert deps.version_at_least((1, 10, 0), (1, 2, 0))  # numeric, not lexical
        assert not deps.version_at_least((1, 1, 6), (1, 2, 0))
        assert not deps.version_at_least(None, (1, 2, 0))


class TestMiseCmd:
    def test_unpinned_runs_tool_directly(self):
        assert deps.mise_cmd("terragrunt", "", ["plan"]) == ["terragrunt", "plan"]

    def test_pinned(self):
        assert deps.mise_cmd("terragrunt", "1.1.6", ["plan"]) == [
            "mise", "exec", "terragrunt@1.1.6", "--", "terragrunt", "plan"]

    def test_extra_pins_share_one_exec(self):
        assert deps.mise_cmd("terragrunt", "1.1.6", ["plan"], extra=["terraform@1.16.6"]) == [
            "mise", "exec", "terragrunt@1.1.6", "terraform@1.16.6", "--", "terragrunt", "plan"]

    def test_extra_pin_without_tool_pin(self):
        assert deps.mise_cmd("terragrunt", "", ["plan"], extra=["terraform@1.16.6"]) == [
            "mise", "exec", "terraform@1.16.6", "--", "terragrunt", "plan"]

    def test_tf_cmd_pins_terraform_only(self):
        assert deps.tf_cmd("terraform", "1.16.6", ["show"])[:3] == ["mise", "exec", "terraform@1.16.6"]
        assert deps.tf_cmd("tofu", "1.16.6", ["show"]) == ["tofu", "show"]
        assert deps.tf_cmd("terraform", "", ["show"]) == ["terraform", "show"]


class TestResolveTfBinary:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setattr(deps.shutil, "which", lambda c: "/bin/" + c)
        env = {"TG_TF_PATH": "/opt/terraform"}
        assert deps.resolve_tf_binary({"terraform_binary": "tofu"}, env) == "/opt/terraform"
        assert deps.resolve_tf_binary(None, {"TERRAGRUNT_TFPATH": "tf-legacy"}) == "tf-legacy"

    def test_unit_config_next(self, monkeypatch):
        monkeypatch.setattr(deps.shutil, "which", lambda c: None)
        assert deps.resolve_tf_binary({"terraform_binary": "tofu"}, {}) == "tofu"

    def test_default_prefers_tofu_like_terragrunt(self, monkeypatch):
        monkeypatch.setattr(deps.shutil, "which", lambda c: "/bin/tofu" if c == "tofu" else None)
        assert deps.resolve_tf_binary({"terraform_binary": None}, {}) == "tofu"
        monkeypatch.setattr(deps.shutil, "which", lambda c: None)
        assert deps.resolve_tf_binary(None, {}) == "terraform"

    def test_pinned_tf_env(self):
        pinned = SimpleNamespace(terraform_version="1.16.6")
        assert deps.pinned_tf_env(pinned, {})["TG_TF_PATH"] == "terraform"
        # explicit user choice is respected
        assert deps.pinned_tf_env(pinned, {"TG_TF_PATH": "tofu"})["TG_TF_PATH"] == "tofu"
        assert "TG_TF_PATH" not in deps.pinned_tf_env(pinned, {"TERRAGRUNT_TFPATH": "x"})
        assert "TG_TF_PATH" not in deps.pinned_tf_env(SimpleNamespace(terraform_version=""), {})


class TestDependencies:
    def _args(self):
        return SimpleNamespace(terraform_version="", terragrunt_version="")

    def test_tofu_only_is_enough(self, monkeypatch):
        monkeypatch.setattr(deps.shutil, "which",
                            lambda c: "/bin/" + c if c in ("terragrunt", "tofu") else None)
        deps.check_dependencies(self._args())  # no SystemExit

    def test_missing_engine_exits(self, monkeypatch):
        monkeypatch.setattr(deps.shutil, "which", lambda c: "/bin/tg" if c == "terragrunt" else None)
        with pytest.raises(SystemExit):
            deps.check_dependencies(self._args())

    def test_get_versions_labels_opentofu(self, monkeypatch):
        monkeypatch.delenv("TG_TF_PATH", raising=False)
        monkeypatch.delenv("TERRAGRUNT_TFPATH", raising=False)
        monkeypatch.setattr(deps.shutil, "which", lambda c: "/bin/tofu" if c == "tofu" else None)

        def fake_run(cmd, **kw):
            out = {"tofu": "OpenTofu v1.13.1\non darwin_arm64",
                   "terragrunt": "terragrunt version v1.2.0-rc3"}[cmd[0]]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        monkeypatch.setattr(deps.subprocess, "run", fake_run)
        v = deps.get_versions(self._args())
        assert v == {"terragrunt": "v1.2.0-rc3", "terraform": "1.13.1 (OpenTofu)"}
        assert deps.parse_version(v["terragrunt"]) == (1, 2, 0)
