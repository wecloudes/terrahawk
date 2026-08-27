"""Unit tests for terrahawk.discovery (pure DAG builder + filesystem helpers)."""

import os

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
