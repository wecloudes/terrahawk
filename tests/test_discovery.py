"""Unit tests for terrahawk.discovery.build_dag (pure DAG builder)."""

import pytest

from terrahawk import discovery


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
