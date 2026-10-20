"""Unit tests for terrahawk.worker pure helpers.

Covers the cache-dir scorer, per-unit timeout resolution, and plan-arg
assembly — the decision logic inside run_plan, exercised without spawning
terragrunt.
"""

import json
import subprocess
from types import SimpleNamespace

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


class _FakeRun:
    """Records subprocess.run calls and answers like terragrunt/terraform."""

    def __init__(self, render_json):
        self.calls = []
        self.render_json = render_json

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

    def _run(self, tmp_path, monkeypatch, args, render_json=None):
        for k in ("TG_TF_PATH", "TERRAGRUNT_TFPATH"):
            monkeypatch.delenv(k, raising=False)
        fake = _FakeRun(render_json or {})
        monkeypatch.setattr(worker.subprocess, "run", fake)
        unit = self._unit(tmp_path)
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
