"""Unit tests for terrahawk.state_age pure helpers.

Covers the regex/brace-walking core that reconstructs backend config, resolves
locals, and normalises timestamps — the most parse-fragile module in the tree.
Subprocess-backed query functions are exercised only for their pre-CLI parsing
and guard paths (no cloud calls).
"""

import pytest

from terrahawk import state_age as sa


class TestNormalizeIso:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-27T10:00:00+0000", "2026-08-27T10:00:00+00:00"),
        ("2026-08-27T10:00:00-0530", "2026-08-27T10:00:00-05:30"),
        # already colon-separated → untouched
        ("2026-08-27T10:00:00+00:00", "2026-08-27T10:00:00+00:00"),
        # Z / no offset → untouched
        ("2026-08-27T10:00:00Z", "2026-08-27T10:00:00Z"),
        ("2026-08-27T10:00:00", "2026-08-27T10:00:00"),
    ])
    def test_offset_normalisation(self, raw, expected):
        assert sa._normalize_iso(raw) == expected


class TestBraceBlock:
    def test_simple_block(self):
        body, end = sa._brace_block("{a}tail", 1)
        assert body == "a"
        assert "tail" == "{a}tail"[end:]

    def test_nested_braces(self):
        text = "{ a = { b = 1 } c = 2 }rest"
        body, end = sa._brace_block(text, 1)
        assert body == " a = { b = 1 } c = 2 "
        assert text[end:] == "rest"

    def test_unbalanced_walks_to_end(self):
        body, end = sa._brace_block("{ a = 1", 1)
        assert body == " a = 1"
        assert end == len("{ a = 1")


class TestParseHclStringLocals:
    def test_extracts_string_assignments_only(self):
        block = 'env = "prod"\nregion = "westeurope"\ncount = 3\nmap = { x = "y" }'
        got = sa._parse_hcl_string_locals(block)
        assert got == {"env": "prod", "region": "westeurope", "x": "y"}


class TestExtractRemoteStateConfig:
    def test_scopes_to_config_block_not_provider(self):
        content = '''
provider "aws" {
  region = "us-east-1"
}
remote_state {
  backend = "s3"
  config = {
    bucket  = "tf-state-prod"
    key     = "app/terraform.tfstate"
    region  = "eu-west-1"
  }
}
'''
        backend, config_block = sa._extract_remote_state_config(content)
        assert backend == "s3"
        # region inside config wins; provider region must NOT leak in
        assert 'region  = "eu-west-1"' in config_block
        assert "us-east-1" not in config_block
        assert 'bucket  = "tf-state-prod"' in config_block

    def test_nested_map_in_config_not_truncated(self):
        content = '''
remote_state {
  backend = "azurerm"
  config = {
    storage_account_name = "tfstate"
    container_name       = "states"
    extra = { nested = "deep" }
    resource_group_name  = "rg-prod"
  }
}
'''
        backend, config_block = sa._extract_remote_state_config(content)
        assert backend == "azurerm"
        # a nested `{ ... }` before later keys must not cut the block short
        assert "resource_group_name" in config_block

    def test_fallback_when_no_remote_state_block(self):
        content = 'backend = "gcs"\nbucket = "b"'
        backend, config_block = sa._extract_remote_state_config(content)
        assert backend == "gcs"
        assert config_block == content


class TestResolveLocal:
    def test_resolves_from_root_content(self, tmp_path):
        root = tmp_path / "root.hcl"
        root.write_text('locals { Service = "billing" }')
        assert sa._resolve_local("Service", root.read_text(), tmp_path, root) == "billing"

    def test_resolves_from_sibling_hcl(self, tmp_path):
        root = tmp_path / "root.hcl"
        root.write_text('locals {}')
        (tmp_path / "global.hcl").write_text('Customer = "acme"')
        assert sa._resolve_local("Customer", root.read_text(), tmp_path, root) == "acme"

    def test_skips_unresolved_interpolation(self, tmp_path):
        root = tmp_path / "root.hcl"
        root.write_text('Region = "${local.other}"')
        # value still contains ${...} → not a concrete resolution
        assert sa._resolve_local("Region", root.read_text(), tmp_path, root) == ""

    def test_missing_returns_empty(self, tmp_path):
        root = tmp_path / "root.hcl"
        root.write_text('locals {}')
        assert sa._resolve_local("Nope", root.read_text(), tmp_path, root) == ""


class TestResolveS3StaticPrefix:
    def _root(self, tmp_path, body='locals { Service = "billing" Env = "prod" }'):
        root = tmp_path / "root.hcl"
        root.write_text(body)
        return root

    def test_single_local_segment(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "${local.Service}/${path_relative_to_include()}/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == "billing/"

    def test_literal_plus_local_multi_segment(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "env/${local.Service}/${path_relative_to_include()}/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == "env/billing/"

    def test_two_local_segments(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "${local.Env}/${local.Service}/${path_relative_to_include()}/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == "prod/billing/"

    def test_purely_literal_prefix(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "terraform/state/${path_relative_to_include()}/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == "terraform/state/"

    def test_no_static_prefix(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "${path_relative_to_include()}/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == ""

    def test_no_dynamic_segment_returns_empty(self, tmp_path):
        root = self._root(tmp_path)
        # fixed single-unit key: no path_relative_to_include() → not a shared prefix
        tpl = "app/terraform.tfstate"
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == ""

    def test_unresolvable_local_bails(self, tmp_path):
        root = self._root(tmp_path)
        tpl = "${local.Unknown}/${path_relative_to_include()}/terraform.tfstate"
        # unresolved interpolation → "" rather than a broken prefix
        assert sa._resolve_s3_static_prefix(tpl, root.read_text(), tmp_path, root) == ""


class TestExtractRootProviderTemplate:
    def test_extracts_heredoc_contents(self, tmp_path):
        (tmp_path / "root.hcl").write_text('''
generate "provider" {
  path      = "provider.tf"
  contents  = <<EOF
provider "aws" {
  region = "eu-west-1"
}
EOF
}
''')
        tpl = sa.extract_root_provider_template(tmp_path)
        assert 'provider "aws"' in tpl
        assert "eu-west-1" in tpl

    def test_absent_returns_empty(self, tmp_path):
        (tmp_path / "root.hcl").write_text('locals {}')
        assert sa.extract_root_provider_template(tmp_path) == ""


class TestAzureAuthArgs:
    def test_login_mode_by_default(self):
        assert sa._azure_auth_args({}) == ["--auth-mode", "login"]

    @pytest.mark.parametrize("var", [
        "AZURE_STORAGE_KEY",
        "AZURE_STORAGE_ACCOUNT_KEY",
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_STORAGE_SAS_TOKEN",
    ])
    def test_explicit_credential_uses_default_auth(self, var):
        assert sa._azure_auth_args({var: "x"}) == []

    def test_empty_credential_var_ignored(self):
        # a var present but empty must not switch away from login mode
        assert sa._azure_auth_args({"AZURE_STORAGE_KEY": ""}) == ["--auth-mode", "login"]


class TestQueryBlobDatesGuards:
    def test_no_root_hcl_returns_empty(self, tmp_path, capsys):
        assert sa.query_blob_dates(tmp_path) == {}
        assert "No root.hcl" in capsys.readouterr().out

    def test_missing_cli_skips_gracefully(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "root.hcl").write_text('''
remote_state {
  backend = "s3"
  config = { bucket = "b" key = "k/terraform.tfstate" }
}
''')
        monkeypatch.setattr(sa.shutil, "which", lambda _cli: None)
        assert sa.query_blob_dates(tmp_path) == {}
        assert "aws CLI not found" in capsys.readouterr().out
