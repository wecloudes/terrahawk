"""Unit tests for terrahawk.discovery (pure DAG builder + filesystem helpers)."""

import os
import subprocess

import pytest

from terrahawk import discovery


class TestFindStackFiles:
    def test_finds_source_stack_roots_only(self, tmp_path):
        (tmp_path / "envA").mkdir()
        (tmp_path / "envA" / "terragrunt.stack.hcl").write_text("")
        # generated artifacts must be ignored
        gen = tmp_path / "envA" / ".terragrunt-stack" / "nested"
        gen.mkdir(parents=True)
        (gen / "terragrunt.stack.hcl").write_text("")
        cache = tmp_path / ".terragrunt-cache" / "x"
        cache.mkdir(parents=True)
        (cache / "terragrunt.stack.hcl").write_text("")

        found = discovery.find_stack_files(tmp_path)
        assert found == [tmp_path / "envA"]

    def test_none_returns_empty(self, tmp_path):
        assert discovery.find_stack_files(tmp_path) == []


class TestDiscoverRglob:
    def test_skips_root_cache_and_stack(self, tmp_path):
        (tmp_path / "terragrunt.hcl").write_text("")          # root — skipped
        for rel in ("dev/vpc", "dev/app"):
            d = tmp_path / rel
            d.mkdir(parents=True)
            (d / "terragrunt.hcl").write_text("")
        for junk in (".terragrunt-cache/z", "dev/app/.terragrunt-stack/gen"):
            d = tmp_path / junk
            d.mkdir(parents=True)
            (d / "terragrunt.hcl").write_text("")

        units = discovery._discover_rglob(tmp_path)
        rels = sorted(rp for _, rp in units)
        assert rels == ["dev/app", "dev/vpc"]
        # absolute dirs are realpath-resolved
        assert all(os.path.isabs(ud) for ud, _ in units)


class TestIsAlwaysSkipped:
    @pytest.mark.parametrize("rel", [
        ".migration-backup/eysa/production/vpc",  # hidden dir at root
        "eysa/.bak/vpc",                          # hidden dir mid-path
        ".git/x",
        "catalog/units/vpc",                      # module catalog
        "eysa/catalog/vpc",                       # catalog mid-path
    ])
    def test_skipped(self, rel):
        assert discovery._is_always_skipped(rel) is True

    @pytest.mark.parametrize("rel", [
        "eysa/production/eu-west-1/vpc",
        "eysa/production/eu-west-1/.terragrunt-stack/vpc",       # generated stack unit kept
        "eysa/production/eu-west-1/.terragrunt-stack/services/keycloak",
        "catalogue/vpc",                                         # substring, not a segment
    ])
    def test_kept(self, rel):
        assert discovery._is_always_skipped(rel) is False


class TestParseDepsRegex:
    def test_extracts_dependency_config_paths(self, tmp_path):
        vpc = tmp_path / "vpc"; app = tmp_path / "app"
        vpc.mkdir(); app.mkdir()
        (vpc / "terragrunt.hcl").write_text("")
        (app / "terragrunt.hcl").write_text(
            'dependency "vpc" {\n  config_path = "../vpc"\n}\n'
        )
        units = [
            (os.path.realpath(str(vpc)), "vpc"),
            (os.path.realpath(str(app)), "app"),
        ]
        deps = discovery._parse_deps_regex(units)
        assert deps[os.path.realpath(str(app))] == {os.path.realpath(str(vpc))}

    def test_dep_outside_scanned_set_dropped(self, tmp_path):
        app = tmp_path / "app"; app.mkdir()
        (app / "terragrunt.hcl").write_text(
            'dependency "ext" {\n  config_path = "../not-scanned"\n}\n'
        )
        units = [(os.path.realpath(str(app)), "app")]
        deps = discovery._parse_deps_regex(units)
        # only in-set deps are kept → app has no recorded deps
        assert deps.get(os.path.realpath(str(app)), set()) == set()


@pytest.mark.skipif(
    not hasattr(discovery, "build_dag"),
    reason="build_dag not present",
)
class TestBuildDag:
    def test_no_deps_single_wave(self):
        units = [("/a", "a"), ("/b", "b"), ("/c", "c")]
        waves = discovery.build_dag(units, deps={})
        assert len(waves) == 1
        assert set(waves[0]) == {"/a", "/b", "/c"}

    def test_linear_dependency_ordered_waves(self):
        # /c depends on /b depends on /a  ->  three ordered waves
        units = [("/a", "a"), ("/b", "b"), ("/c", "c")]
        deps = {"/b": {"/a"}, "/c": {"/b"}}
        waves = discovery.build_dag(units, deps=deps)
        assert len(waves) == 3
        assert waves[0] == ["/a"]
        assert waves[1] == ["/b"]
        assert waves[2] == ["/c"]

    def test_dependency_on_unscanned_unit_ignored(self):
        # /b depends on /x which is not among the scanned units -> treated as root
        units = [("/a", "a"), ("/b", "b")]
        deps = {"/b": {"/x"}}
        waves = discovery.build_dag(units, deps=deps)
        assert len(waves) == 1
        assert set(waves[0]) == {"/a", "/b"}

    def test_diamond_dependency(self):
        # d depends on b and c; both depend on a
        units = [("/a", "a"), ("/b", "b"), ("/c", "c"), ("/d", "d")]
        deps = {"/b": {"/a"}, "/c": {"/a"}, "/d": {"/b", "/c"}}
        waves = discovery.build_dag(units, deps=deps)
        assert waves[0] == ["/a"]
        assert set(waves[1]) == {"/b", "/c"}
        assert waves[-1] == ["/d"]

    def test_circular_deps_appended_as_final_wave(self):
        units = [("/a", "a"), ("/b", "b")]
        deps = {"/a": {"/b"}, "/b": {"/a"}}
        waves = discovery.build_dag(units, deps=deps)
        # cycle members can't be topologically ordered; they land in a trailing wave
        assert set(waves[-1]) == {"/a", "/b"}


class TestUnitPathFilters:
    """--unit pre-narrowing: candidate path filters for `terragrunt find`."""

    def _tree(self, tmp_path):
        for rel in ("prod/westeurope/app-gateway", "prod/northeurope/app-gateway",
                    "shared/vpc", "dev/vpc"):
            d = tmp_path / rel
            d.mkdir(parents=True)
            (d / "terragrunt.hcl").write_text("")

    def test_exact_and_suffix_matches(self, tmp_path):
        self._tree(tmp_path)
        # exact rel_path
        assert discovery.unit_path_filters(tmp_path, "shared/vpc") == ["./shared/vpc"]
        # suffix matches every unit of that name, wherever it lives
        assert discovery.unit_path_filters(tmp_path, "app-gateway") == [
            "./prod/northeurope/app-gateway", "./prod/westeurope/app-gateway"]
        # a bare name matching nothing narrows to nothing (None -> full discovery)
        assert discovery.unit_path_filters(tmp_path, "nope") is None

    def test_strips_and_rejects_needles(self, tmp_path):
        self._tree(tmp_path)
        assert discovery.unit_path_filters(tmp_path, "/shared/vpc/") == ["./shared/vpc"]
        assert discovery.unit_path_filters(tmp_path, "") is None
        assert discovery.unit_path_filters(tmp_path, None) is None
        assert discovery.unit_path_filters(tmp_path, "/") is None

    def test_root_config_and_cache_pruned(self, tmp_path):
        (tmp_path / "terragrunt.hcl").write_text("")          # root — never a unit
        stack = tmp_path / "stack" / ".terragrunt-stack" / "services" / "api"
        stack.mkdir(parents=True)
        (stack / "terragrunt.hcl").write_text("")             # stack member — real unit
        cache = tmp_path / "prod" / ".terragrunt-cache" / "xyz" / "app-gateway"
        cache.mkdir(parents=True)
        (cache / "terragrunt.hcl").write_text("")             # cache — pruned from the walk

        filters = discovery.unit_path_filters(tmp_path, "api")
        assert filters == ["./stack/.terragrunt-stack/services/api"]
        # the walk never descends into .terragrunt-cache: a needle matching only
        # a cache path finds nothing
        assert discovery.unit_path_filters(tmp_path, "app-gateway") is None

    def test_duplicate_matches_deduped(self, tmp_path):
        self._tree(tmp_path)
        # exact + suffix can't both apply to different files, but the same
        # rel_path produced twice (impossible via os.walk) is guarded by set()
        filters = discovery.unit_path_filters(tmp_path, "vpc")
        assert sorted(filters) == ["./dev/vpc", "./shared/vpc"]


class TestDiscoverNativeUnitFilters:
    """`--filter` path expressions reach the find command, and a rejected
    filter retries unfiltered instead of falling back to rglob."""

    @staticmethod
    def _make_find(monkeypatch, tmp_path, responses):
        """responses: list of (rc, stdout) answered in order per invocation."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            rc, out = responses[len(calls) - 1] if len(calls) <= len(responses) else responses[-1]
            return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

        monkeypatch.setattr(discovery.subprocess, "run", fake_run)
        return calls

    UNIT_JSON = '[{"type": "unit", "path": "prod/app", "dependencies": []}]'

    def test_unit_filters_passed_as_repeated_filter_flags(self, monkeypatch, tmp_path):
        calls = self._make_find(monkeypatch, tmp_path, [(0, self.UNIT_JSON)])
        units, deps = discovery.discover_units(
            tmp_path, tg_ver="", filter_expr=None,
            unit_filters=["./prod/app", "./dev/vpc"])
        assert [rp for _, rp in units] == ["prod/app"]
        joined = " ".join(calls[0])
        assert "--filter=./prod/app" in joined and "--filter=./dev/vpc" in joined

    def test_rejected_filter_retries_unfiltered(self, monkeypatch, tmp_path):
        calls = self._make_find(monkeypatch, tmp_path,
                                [(1, ""),                      # narrowed find fails
                                 (0, self.UNIT_JSON)])         # retry without filters
        units, deps = discovery.discover_units(
            tmp_path, tg_ver="", filter_expr=None, unit_filters=["./prod/we|rd"])
        assert [rp for _, rp in units] == ["prod/app"]
        assert len(calls) == 2
        assert "--filter" not in " ".join(calls[1])
        # deps survived the retry: native discovery, not the rglob fallback
        assert deps is not None

    def test_no_filters_single_invocation(self, monkeypatch, tmp_path):
        calls = self._make_find(monkeypatch, tmp_path, [(0, self.UNIT_JSON)])
        discovery.discover_units(tmp_path)
        assert len(calls) == 1

    def test_double_failure_falls_back_to_rglob(self, monkeypatch, tmp_path):
        (tmp_path / "dev" / "app").mkdir(parents=True)
        (tmp_path / "dev" / "app" / "terragrunt.hcl").write_text("")
        calls = self._make_find(monkeypatch, tmp_path, [(1, ""), (1, "")])
        units, deps = discovery.discover_units(
            tmp_path, tg_ver="", filter_expr=None, unit_filters=["./prod/app"])
        assert [rp for _, rp in units] == ["dev/app"]
        assert deps is None  # rglob fallback
        assert len(calls) == 2
