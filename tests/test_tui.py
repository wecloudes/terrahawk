"""TUI formatters (no curses screen needed)."""

from terrahawk.tui.common import _enc_suffix
from terrahawk.tui.detail import _format_detail
from terrahawk.tui.listing import _apply_filters, _build_rows


def _unit(**kw):
    u = {"unit": "prod/sub/eu-west-1/app", "displayUnit": "prod/sub/eu-west-1/app",
         "environment": "prod", "subscription": "sub", "status": "drift",
         "planResources": [], "driftedResources": []}
    u.update(kw)
    return u


def _text(lines):
    return "\n".join(t for _, t in lines)


def test_detail_shows_trend_owners_ignored_and_throttling():
    txt = _text(_format_detail(_unit(trend="new", prevStatus="clean", owners=["@org/a", "@b"],
                                     ignoredCount=2, ignoredStatus="drift", throttleRetries=1)))
    assert "Since last run: NEW (was clean)" in txt
    assert "Owners: @org/a @b" in txt
    assert "Ignored: 2 change(s) by .terrahawkignore — would be DRIFT" in txt
    assert "re-planned 1x" in txt


def test_detail_persisting_and_resolved():
    assert "persisting since 2026-10-01" in _text(_format_detail(
        _unit(trend="persisting", since="2026-10-01")))
    assert "resolved (was drift)" in _text(_format_detail(
        _unit(status="clean", trend="resolved", prevStatus="drift")))


def test_resource_markers():
    assert _enc_suffix({}) == ""
    s = _enc_suffix({"encodingOnly": ["user_data"], "isNew": True, "ignored": "tags"})
    assert "[gzip re-encode only]" in s and "[new]" in s and "[ignored: tags]" in s


def test_detail_lists_resources_with_markers():
    u = _unit(planResources=[{"address": "aws_s3_bucket.a", "action": "update", "isNew": True}],
              driftedResources=[{"address": "aws_sg.x", "actions": ["update"], "ignored": "sg"}])
    txt = _text(_format_detail(u))
    assert "aws_s3_bucket.a  [new]" in txt and "aws_sg.x  [ignored: sg]" in txt


def test_list_rows_group_units():
    units = [_unit(), _unit(unit="dev/sub/x", displayUnit="dev/sub/x", environment="dev",
                            status="clean")]
    filtered = _apply_filters(units, None, None, None, "", "status")
    rows = _build_rows(filtered)
    assert [r[0] for r in rows].count("unit") == 2
