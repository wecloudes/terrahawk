"""Slack / Teams webhook alerts."""

import json
import urllib.error

import pytest

from terrahawk import notify

SLACK = "https://hooks.slack.com/services/T000/B000/XXXX"
TEAMS = "https://prod-00.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"


def _results():
    return [
        {"unit": "prod/app", "status": "drift", "trend": "new", "owners": ["@org/prod"],
         "planResources": [{"address": "a", "action": "update"},
                           {"address": "b", "action": "update", "ignored": "tags"}]},
        {"unit": "prod/db", "status": "error", "trend": "persisting", "errorClass": "auth"},
        {"unit": "prod/ok", "status": "clean", "trend": "resolved"},
    ]


COUNTS = {"new": 1, "persisting": 1, "resolved": 1, "newDrift": 1, "newError": 0}


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, payload: sent.append((url, payload)) or 200)
    return sent


class TestTrigger:
    def test_no_url_no_send(self, posts):
        assert notify.maybe_notify(_results(), COUNTS, "new", env={}) is False
        assert posts == []

    def test_new_mode_needs_new_findings(self, posts):
        quiet = [dict(r, trend="persisting") for r in _results()]
        assert notify.maybe_notify(quiet, COUNTS, "new", env={notify.ENV_URL: SLACK}) is False
        assert notify.maybe_notify(quiet, COUNTS, "findings", env={notify.ENV_URL: SLACK}) is True

    def test_never_and_non_https(self, posts, capsys):
        assert notify.maybe_notify(_results(), COUNTS, "never", env={notify.ENV_URL: SLACK}) is False
        assert notify.maybe_notify(_results(), COUNTS, "new",
                                   env={notify.ENV_URL: "http://hooks.slack.com/x"}) is False
        assert posts == []


class TestPayloads:
    def test_slack(self, posts):
        assert notify.maybe_notify(_results(), COUNTS, "new", "https://r.example/", "rep.html",
                                   "prev.json", env={notify.ENV_URL: SLACK})
        url, payload = posts[0]
        assert url == SLACK and "blocks" in payload
        text = json.dumps(payload)
        assert "prod/app" in text and "1 change(s)" in text and "@org/prod" in text
        assert "prod/db" not in text                       # persisting, not new
        assert "https://r.example/rep.html" in text and "prev.json" in text

    def test_teams_adaptive_card(self, posts):
        assert notify.maybe_notify(_results(), COUNTS, "findings", env={notify.ENV_URL: TEAMS})
        payload = posts[0][1]
        att = payload["attachments"][0]
        assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
        body = json.dumps(att["content"]["body"])
        assert "prod/app" in body and "prod/db" in body

    def test_kind_override(self, posts):
        notify.maybe_notify(_results(), COUNTS, "new",
                            env={notify.ENV_URL: TEAMS, notify.ENV_KIND: "slack"})
        assert "blocks" in posts[0][1]


class TestFailuresNeverLeakUrl:
    def test_http_error(self, monkeypatch, capsys):
        def boom(url, payload):
            raise urllib.error.HTTPError(url, 404, "nope", {}, None)
        monkeypatch.setattr(notify, "_post", boom)
        assert notify.maybe_notify(_results(), COUNTS, "new", env={notify.ENV_URL: SLACK}) is False
        out = capsys.readouterr().out
        assert "404" in out and SLACK not in out and "XXXX" not in out

    def test_network_error(self, monkeypatch, capsys):
        def boom(url, payload):
            raise OSError(f"cannot reach {url}")
        monkeypatch.setattr(notify, "_post", boom)
        assert notify.maybe_notify(_results(), COUNTS, "new", env={notify.ENV_URL: SLACK}) is False
        assert "XXXX" not in capsys.readouterr().out


def test_report_link():
    assert notify.report_link("", "r.html") == ""
    assert notify.report_link("https://x/reports/", "r.html") == "https://x/reports/r.html"
    assert notify.report_link("https://x/latest.html", "r.html") == "https://x/latest.html"
