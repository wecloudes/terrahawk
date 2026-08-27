"""Unit tests for terrahawk.worker pure helpers.

Covers the cache-dir scorer, per-unit timeout resolution, and plan-arg
assembly — the decision logic inside run_plan, exercised without spawning
terragrunt.
"""

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
