"""Run-over-run comparison (new / persisting / resolved)."""

import json
import os
import time

from terrahawk import history


def _e(unit, status, res=(), since=None, partial=False):
    e = {"unit": unit, "status": status,
         "planResources": [{"address": a, "action": "update"} for a in res],
         "driftedResources": []}
    if since:
        e["since"] = since
    if partial:
        e["partialRun"] = True
    return e


class TestAnnotate:
    def test_trends(self):
        base = [_e("a", "drift", ["x.one"], since="2026-10-01"), _e("b", "drift"),
                _e("c", "clean"), _e("d", "error")]
        cur = [_e("a", "drift", ["x.one", "x.two"]), _e("b", "clean"),
               _e("c", "drift", ["x.three"]), _e("d", "drift"), _e("e", "error")]
        counts = history.annotate(cur, base, "2026-11-03", "2026-11-02")
        t = {e["unit"]: e for e in cur}
        assert t["a"]["trend"] == "persisting" and t["a"]["since"] == "2026-10-01"
        assert [r["isNew"] for r in t["a"]["planResources"]] == [False, True]
        assert t["b"]["trend"] == "resolved" and t["b"]["prevStatus"] == "drift"
        assert t["c"]["trend"] == "new" and t["c"]["since"] == "2026-11-03"
        assert t["c"]["planResources"][0]["isNew"] is True
        assert t["d"]["trend"] == "new"          # error -> drift is a new finding kind
        assert t["e"]["trend"] == "new" and t["e"]["prevStatus"] == ""  # new unit
        assert counts == {"new": 3, "persisting": 1, "resolved": 1,
                          "newDrift": 2, "newError": 1}

    def test_persisting_without_since_uses_baseline_date(self):
        cur = [_e("a", "drift")]
        history.annotate(cur, [_e("a", "drift")], "2026-11-03", "2026-11-02")
        assert cur[0]["since"] == "2026-11-02"

    def test_no_baseline(self):
        cur = [_e("a", "drift", ["x.one"]), _e("b", "clean")]
        counts = history.annotate(cur, None, "2026-11-03")
        assert cur[0]["trend"] == "" and cur[0]["since"] == "2026-11-03"
        assert "isNew" not in cur[0]["planResources"][0]
        assert counts["new"] == 0


class TestLoadBaseline:
    def _write(self, d, name, entries, age=0):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            json.dump(entries, f)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_newest_full_report_skips_partial_and_current(self, tmp_path):
        d = str(tmp_path)
        self._write(d, "terrahawk_20261101_060000.json", [_e("a", "drift")], age=300)
        self._write(d, "terrahawk_20261102_060000.json", [_e("a", "clean", partial=True)], age=200)
        cur = self._write(d, "terrahawk_20261103_060000.json", [], age=0)
        entries, label, date_iso = history.load_baseline(None, d, exclude=cur)
        assert label == "terrahawk_20261101_060000.json"
        assert date_iso == "2026-11-01" and entries[0]["status"] == "drift"

    def test_explicit_path_and_errors(self, tmp_path):
        p = self._write(str(tmp_path), "prev.json", [_e("a", "drift")])
        entries, label, _ = history.load_baseline(p, None)
        assert label == "prev.json" and len(entries) == 1
        assert history.load_baseline(str(tmp_path / "missing.json"), None)[0] is None
        bad = tmp_path / "bad.json"
        bad.write_text('{"not": "a list"}')
        assert history.load_baseline(str(bad), None)[0] is None

    def test_no_reports(self, tmp_path):
        assert history.load_baseline(None, str(tmp_path))[0] is None
        assert history.load_baseline(None, str(tmp_path / "nope"))[0] is None

    def test_url(self, monkeypatch):
        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(history.urllib.request, "urlopen",
                            lambda url, timeout: Resp(json.dumps([_e("a", "drift")]).encode()))
        entries, label, date_iso = history.load_baseline(
            "https://example.test/r/terrahawk_20261102_060000.json", None)
        assert len(entries) == 1 and date_iso == "2026-11-02"

        def boom(url, timeout):
            raise OSError("down")
        monkeypatch.setattr(history.urllib.request, "urlopen", boom)
        entries, reason, _ = history.load_baseline("https://example.test/x.json", None)
        assert entries is None and "OSError" in reason
