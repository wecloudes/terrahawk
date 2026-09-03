"""Unit tests for terrahawk.profiles (multi-account profile mapping)."""

import os

from terrahawk import profiles


class TestExtractAccount:
    def test_get_env_default(self, tmp_path):
        f = tmp_path / "env.hcl"
        f.write_text('locals {\n  aws_account_id = get_env("ATM_AWS_ACCOUNT_ID", "533267349964")\n}\n')
        assert profiles._extract_account(str(f)) == "533267349964"

    def test_get_env_var_wins(self, tmp_path, monkeypatch):
        f = tmp_path / "env.hcl"
        f.write_text('aws_account_id = get_env("ATM_AWS_ACCOUNT_ID", "533267349964")\n')
        monkeypatch.setenv("ATM_AWS_ACCOUNT_ID", "111111111111")
        assert profiles._extract_account(str(f)) == "111111111111"

    def test_literal(self, tmp_path):
        f = tmp_path / "env.hcl"
        f.write_text('aws_account_id = "404210446414"\n')
        assert profiles._extract_account(str(f)) == "404210446414"

    def test_local_ref(self, tmp_path):
        (tmp_path / "customer.hcl").write_text('locals {\n  acct = "222222222222"\n}\n')
        f = tmp_path / "env.hcl"
        f.write_text('aws_account_id = local.acct\n')
        assert profiles._extract_account(str(f)) == "222222222222"

    def test_missing(self, tmp_path):
        f = tmp_path / "env.hcl"
        f.write_text('locals {\n  region = "eu-west-1"\n}\n')
        assert profiles._extract_account(str(f)) is None


class TestFindEnvHcl:
    def test_nearest_ancestor(self, tmp_path):
        cfg = tmp_path
        env = cfg / "eysa" / "preproduction"
        unit = env / "eu-west-1" / ".terragrunt-stack" / "vpc"
        unit.mkdir(parents=True)
        (env / "env.hcl").write_text('aws_account_id = "533267349964"\n')
        found = profiles._find_env_hcl(str(unit), str(cfg))
        assert found == str(env / "env.hcl")

    def test_none_above_config(self, tmp_path):
        cfg = tmp_path / "config"
        unit = cfg / "a" / "b"
        unit.mkdir(parents=True)
        # env.hcl OUTSIDE config_dir must not be picked up
        (tmp_path / "env.hcl").write_text('aws_account_id = "1"\n')
        assert profiles._find_env_hcl(str(unit), str(cfg)) is None


class TestAccountToProfile:
    def test_first_profile_wins_shared_account(self):
        m = profiles._account_to_profile(["a", "b"], {"a": "123", "b": "123"})
        assert m == {"123": "a"}

    def test_maps_distinct(self):
        m = profiles._account_to_profile(["pro", "pre"], {"pro": "404", "pre": "533"})
        assert m == {"404": "pro", "533": "pre"}


class TestBuildUnitProfileMap:
    def test_single_profile_shortcircuits(self, tmp_path):
        # No STS, every unit maps to the lone profile regardless of account.
        units = [("/x/a", "a"), ("/x/b", "b")]
        umap, acct_prof = profiles.build_unit_profile_map(units, str(tmp_path), ["only"])
        assert umap == {"a": "only", "b": "only"}
        assert acct_prof == {}

    def test_no_profiles_empty(self, tmp_path):
        assert profiles.build_unit_profile_map([("/x/a", "a")], str(tmp_path), []) == ({}, {})

    def test_multi_profile_matches_by_account(self, tmp_path, monkeypatch):
        cfg = tmp_path
        for env, acct in (("eysa/production", "404210446414"),
                          ("eysa/preproduction", "533267349964")):
            d = cfg / env
            (d / "eu-west-1").mkdir(parents=True)
            (d / "env.hcl").write_text(f'aws_account_id = "{acct}"\n')
        units = [
            (str(cfg / "eysa/production/eu-west-1"), "eysa/production/eu-west-1"),
            (str(cfg / "eysa/preproduction/eu-west-1"), "eysa/preproduction/eu-west-1"),
        ]
        # Stub STS so the test is hermetic.
        monkeypatch.setattr(profiles, "profile_accounts",
                            lambda names: {"atm-pro": "404210446414", "atm-pre": "533267349964"})
        umap, acct_prof = profiles.build_unit_profile_map(units, str(cfg), ["atm-pro", "atm-pre"])
        assert umap == {
            "eysa/production/eu-west-1": "atm-pro",
            "eysa/preproduction/eu-west-1": "atm-pre",
        }
        assert acct_prof == {"404210446414": "atm-pro", "533267349964": "atm-pre"}


class TestStateBucketProfile:
    def test_reads_root_hcl(self, tmp_path):
        (tmp_path / "root.hcl").write_text('locals {\n  state_account_id = "404210446414"\n}\n')
        acct_prof = {"404210446414": "atm-pro", "533267349964": "atm-pre"}
        assert profiles.state_bucket_profile(str(tmp_path), acct_prof, ["atm-pre", "atm-pro"]) == "atm-pro"

    def test_falls_back_to_first(self, tmp_path):
        assert profiles.state_bucket_profile(str(tmp_path), {}, ["atm-pre", "atm-pro"]) == "atm-pre"
