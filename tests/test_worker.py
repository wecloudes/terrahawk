"""Unit tests for terrahawk.worker pure helpers.

Covers the cache-dir scorer, per-unit timeout resolution, and plan-arg
assembly — the decision logic inside run_plan, exercised without spawning
terragrunt.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from terrahawk import worker


class TestFindCacheDir:
    def _mk(self, base, rel, files):
        d = base / rel
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_text("# tf\n")
        return d

    def test_no_cache_root_returns_none(self, tmp_path):
        assert worker.find_cache_dir(str(tmp_path)) is None

    def test_prefers_generated_plus_module(self, tmp_path):
        cache = tmp_path / ".terragrunt-cache"
        # score 1: module only
        self._mk(cache, "aaa/modonly", ["main.tf", "variables.tf"])
        # score 3: generated + module
        best = self._mk(cache, "bbb/full", ["provider.tf", "main.tf"])
        assert worker.find_cache_dir(str(tmp_path)) == str(best)

    def test_generated_only_beats_module_only(self, tmp_path):
        cache = tmp_path / ".terragrunt-cache"
        self._mk(cache, "m", ["main.tf"])            # score 1
        gen = self._mk(cache, "g", ["backend.tf"])   # score 2 (generated only)
        assert worker.find_cache_dir(str(tmp_path)) == str(gen)

    def test_examples_and_test_dirs_excluded(self, tmp_path):
        cache = tmp_path / ".terragrunt-cache"
        self._mk(cache, "mod/examples/full", ["provider.tf", "main.tf"])
        self._mk(cache, "mod/test/full", ["provider.tf", "main.tf"])
        real = self._mk(cache, "mod/real", ["provider.tf", "main.tf"])
        assert worker.find_cache_dir(str(tmp_path)) == str(real)

    def test_no_tf_files_returns_none(self, tmp_path):
        cache = tmp_path / ".terragrunt-cache"
        self._mk(cache, "empty", ["readme.md"])
        assert worker.find_cache_dir(str(tmp_path)) is None


class TestResolveTimeout:
    def test_default_when_no_pattern_matches(self):
        assert worker._resolve_timeout("/x/prod/app", 300, {}) == 300
        assert worker._resolve_timeout("/x/prod/app", 300, {"staging": 60}) == 300

    def test_first_substring_pattern_wins(self):
        ut = {"prod": 900, "app": 120}
        # dict preserves insertion order; "prod" matches first
        assert worker._resolve_timeout("/x/prod/app", 300, ut) == 900

    def test_pattern_is_substring_match(self):
        assert worker._resolve_timeout("/env/westeurope/db", 300, {"westeurope": 600}) == 600


class TestBuildPlanArgs:
    def test_base_args_read_only(self):
        args = worker._build_plan_args("/u", "/tmp/p.tfplan")
        assert args[0] == "plan"
        assert "-detailed-exitcode" in args
        assert "-lock=false" in args
        assert "-out=/tmp/p.tfplan" in args
        assert "--working-dir=/u" in args
        assert "--dependency-fetch-output-from-state" in args
        # no-hooks flags absent by default
        assert "--no-hooks" not in args

    def test_no_hooks_prepended(self):
        args = worker._build_plan_args("/u", "/tmp/p.tfplan", no_hooks=True)
        assert "--experiment=optional-hooks" in args
        assert "--no-hooks" in args
        # inserted right after the "plan" subcommand
        assert args.index("--experiment=optional-hooks") == 1
        assert args.index("--no-hooks") == 2

    def test_pre_1_2_keeps_experiment_flags(self):
        args = worker._build_plan_args("/u", "/tmp/p.tfplan", no_hooks=True, tg_version=(1, 1, 6))
        assert "--dependency-fetch-output-from-state" in args
        assert args[1:3] == ["--experiment=optional-hooks", "--no-hooks"]

    def test_1_2_drops_graduated_flags(self):
        # Terragrunt 1.2: fetch-from-state is the default (flag deprecated,
        # errors under strict mode); --no-hooks no longer needs the experiment.
        args = worker._build_plan_args("/u", "/tmp/p.tfplan", no_hooks=True, tg_version=(1, 2, 0))
        assert "--dependency-fetch-output-from-state" not in args
        assert not any(a.startswith("--experiment") for a in args)
        assert args.index("--no-hooks") == 1
        assert "-lock=false" in args and "-detailed-exitcode" in args

    def test_unknown_version_is_legacy_safe(self):
        assert worker._fetch_from_state_args(None) == ["--dependency-fetch-output-from-state"]
        assert worker._fetch_from_state_args((1, 3, 0)) == []


class TestProviderCacheEnv:
    def test_tg_cache_on_1x(self):
        env = worker._provider_cache_env(
            {"TF_PLUGIN_CACHE_DIR": "/cache/plugins",
             "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "true"}, (1, 1, 6))
        assert env == {"TG_PROVIDER_CACHE": "1"}  # Docker's global plugin dir dropped

    def test_opt_out_uses_plugin_dir(self):
        env = worker._provider_cache_env({}, (1, 1, 6), enabled=False)
        assert "TG_PROVIDER_CACHE" not in env
        assert env["TF_PLUGIN_CACHE_DIR"].endswith(".terraform.d/plugin-cache")
        assert env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] == "true"

    def test_user_env_disable_is_respected(self):
        env = worker._provider_cache_env({"TG_PROVIDER_CACHE": "0"}, (1, 1, 6))
        assert env["TG_PROVIDER_CACHE"] == "0" and "TF_PLUGIN_CACHE_DIR" in env

    @pytest.mark.parametrize("ver", [None, (0, 99, 0)])
    def test_old_or_unknown_terragrunt_uses_plugin_dir(self, ver):
        assert not worker.uses_provider_cache({}, ver)
        assert "TF_PLUGIN_CACHE_DIR" in worker._provider_cache_env({}, ver)


class TestRetryableInitErrors:
    def test_terraform_checksum_wording_retries(self):
        # verbatim from a cold-cache parallel run (Terraform 1.16.6)
        out = ("Error: Required plugins are not installed\n- registry.terraform.io/hashicorp/null: "
               "the cached package for registry.terraform.io/hashicorp/null 3.3.2 (in "
               ".terraform/providers) does not match any of the checksums recorded in the "
               "dependency lock file")
        assert worker._is_retryable_init_error(out)
        assert worker._is_retryable_init_error("does not match any of the checksums recorded")

    def test_real_plan_errors_do_not_retry(self):
        assert not worker._is_retryable_init_error('Error: Invalid reference\n  on main.tf line 3')


class TestInfoArgs:
    def test_pre_1_2_uses_experiment(self):
        args = worker._info_args("/u", (1, 1, 6))
        assert args[:3] == ["info", "print", "--experiment=optional-dependency-outputs"]
        assert "--no-dependency-outputs" in args and "--working-dir=/u" in args

    def test_1_2_flag_is_ga(self):
        args = worker._info_args("/u", (1, 2, 0))
        assert not any(a.startswith("--experiment") for a in args)
        assert "--no-dependency-outputs" in args


class TestResolveCacheDir:
    def test_info_working_dir_wins(self, tmp_path):
        wd = tmp_path / ".terragrunt-cache" / "a" / "b"
        wd.mkdir(parents=True)
        (wd / "main.tf").write_text("")
        # a higher-scoring decoy the heuristic would pick
        decoy = tmp_path / ".terragrunt-cache" / "a" / "b" / "modules" / "sub"
        decoy.mkdir(parents=True)
        (decoy / "provider.tf").write_text("")
        (decoy / "main.tf").write_text("")
        assert worker._resolve_cache_dir(str(tmp_path), {"working_dir": str(wd)}) == str(wd)

    def test_unit_without_source_runs_in_unit_dir(self, tmp_path):
        (tmp_path / "terragrunt.hcl").write_text("")
        (tmp_path / "backend.tf").write_text("")
        assert worker._resolve_cache_dir(str(tmp_path), {"working_dir": str(tmp_path)}) == str(tmp_path)
        assert worker.find_cache_dir(str(tmp_path)) is None  # heuristic alone misses it

    def test_falls_back_to_heuristic(self, tmp_path):
        mod = tmp_path / ".terragrunt-cache" / "x"
        mod.mkdir(parents=True)
        (mod / "provider.tf").write_text("")
        (mod / "main.tf").write_text("")
        assert worker._resolve_cache_dir(str(tmp_path), {}) == str(mod)
        assert worker._resolve_cache_dir(str(tmp_path), {"working_dir": "/nope"}) == str(mod)


class _FakeRun:
    """Records subprocess.run calls and answers like terragrunt/terraform."""

    def __init__(self, render_json, info_json=None):
        self.calls = []
        self.render_json = render_json
        self.info_json = info_json

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = ""
        rc = 0
        if "plan" in cmd:
            # terragrunt writes the plan file and exits 2 (drift)
            plan_out = next(a for a in cmd if a.startswith("-out="))[len("-out="):]
            open(plan_out, "w").write("plan")
            rc = 2
        elif "render" in cmd:
            out = json.dumps(self.render_json)
        elif "info" in cmd:
            if self.info_json is None:
                rc = 1  # e.g. experiment unknown on this terragrunt
            else:
                out = json.dumps(self.info_json)
        elif "show" in cmd or "output" in cmd:
            out = json.dumps({"values": {"root_module": {"resources": [{}]}}})
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def tf_calls(self):
        return [(c, kw) for c, kw in self.calls if "show" in c or "output" in c]


class TestRunPlanToolCalls:
    def _unit(self, tmp_path):
        unit = tmp_path / "acct-b" / "app"
        mod = unit / ".terragrunt-cache" / "x" / "y"
        mod.mkdir(parents=True)
        (mod / "provider.tf").write_text("")
        (mod / "main.tf").write_text("")
        return unit

    def _args(self, **kw):
        base = dict(unit_profiles={}, terraform_version="", terragrunt_version="",
                    no_hooks=False, tg_version=(1, 1, 6))
        base.update(kw)
        return SimpleNamespace(**base)

    def _run(self, tmp_path, monkeypatch, args, render_json=None, info_json=None):
        for k in ("TG_TF_PATH", "TERRAGRUNT_TFPATH", "TG_PROVIDER_CACHE"):
            monkeypatch.delenv(k, raising=False)
        unit = self._unit(tmp_path)
        if callable(info_json):
            info_json = info_json(unit)
        fake = _FakeRun(render_json or {}, info_json)
        monkeypatch.setattr(worker.subprocess, "run", fake)
        result = worker.run_plan(str(unit), "acct-b/app", 60, args, {}, str(tmp_path),
                                 str(tmp_path), 0)
        return fake, result

    def test_show_and_output_get_unit_profile(self, tmp_path, monkeypatch):
        args = self._args(unit_profiles={"acct-b/app": "acct-b-profile"})
        fake, result = self._run(tmp_path, monkeypatch, args)
        tf_calls = fake.tf_calls()
        assert len(tf_calls) == 3  # show plan, show state, output
        for _, kw in tf_calls:
            assert kw["env"]["AWS_PROFILE"] == "acct-b-profile"
        assert "plan_json" in result and "state_json" in result and "outputs_json" in result

    def test_show_uses_unit_terraform_binary(self, tmp_path, monkeypatch):
        fake, _ = self._run(tmp_path, monkeypatch, self._args(),
                            render_json={"terraform_binary": "tofu"})
        assert {c[0] for c, _ in fake.tf_calls()} == {"tofu"}

    def test_pinned_terraform_reaches_terragrunt(self, tmp_path, monkeypatch):
        args = self._args(terraform_version="1.16.6")
        fake, _ = self._run(tmp_path, monkeypatch, args)
        plan_cmd, plan_kw = next((c, kw) for c, kw in fake.calls if "plan" in c)
        assert plan_cmd[:4] == ["mise", "exec", "terraform@1.16.6", "--"]
        assert plan_kw["env"]["TG_TF_PATH"] == "terraform"
        for cmd, _ in fake.tf_calls():
            assert cmd[:3] == ["mise", "exec", "terraform@1.16.6"]

    def test_render_drops_fetch_flag_on_1_2(self, tmp_path, monkeypatch):
        fake, _ = self._run(tmp_path, monkeypatch, self._args(tg_version=(1, 2, 0)))
        for cmd, _ in fake.calls:
            assert "--dependency-fetch-output-from-state" not in cmd

    def test_info_print_drives_working_dir_and_binary(self, tmp_path, monkeypatch):
        def info(unit):
            wd = unit / ".terragrunt-cache" / "x" / "y"
            return {"working_dir": str(wd), "terraform_binary": "tofu"}
        fake, result = self._run(tmp_path, monkeypatch, self._args(), info_json=info)
        assert result["cache_dir"].endswith("/.terragrunt-cache/x/y")
        for cmd, _ in fake.tf_calls():
            assert cmd[0] == "tofu"
            assert cmd[1] == "-chdir=" + result["cache_dir"]

    def test_plan_runs_with_provider_cache(self, tmp_path, monkeypatch):
        fake, _ = self._run(tmp_path, monkeypatch, self._args())
        _, plan_kw = next((c, kw) for c, kw in fake.calls if "plan" in c)
        assert plan_kw["env"]["TG_PROVIDER_CACHE"] == "1"
        assert "TF_PLUGIN_CACHE_DIR" not in plan_kw["env"]
