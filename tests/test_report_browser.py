"""Browser smoke test for the HTML report, once per theme.

The template is ~1000 lines of inline JS/CSS that changes every release; the
other report tests only check placeholders. This renders a generated report
in headless Chromium and fails on any JS error, missing UI, or a theme that
doesn't apply — catching broken rendering before a release.

Needs the optional `browser` extra and a Chromium build:

    pip install -e '.[test,browser]'
    playwright install chromium

Skipped automatically when Playwright or its browser is unavailable.
"""

from types import SimpleNamespace

import pytest

from terrahawk import report

playwright_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.browser

THEMES = ("dark", "light", "tokyo-night", "cyberpunk")

DIAGRAM = "graph LR\n  n0[\"aws_s3_bucket.a\"]\n  n1[\"aws_iam_role.r\"]\n  n0 --> n1"


def _fixture_data():
    drift = {
        "unit": "prod/sub1/eu-west-1/app", "displayUnit": "prod/sub1/eu-west-1/app",
        "environment": "prod", "subscription": "sub1", "region": "eu-west-1", "application": "app",
        "status": "drift", "isStack": False, "stackName": "",
        "summary": "Plan: 0 to add, 2 to change, 0 to destroy.",
        "diff": "Terraform will perform the following actions:\n  ~ resource \"aws_s3_bucket\" \"a\" {\n      ~ tags = {}\n    }\n",
        "error": "", "errorClass": "",
        "planResources": [
            {"address": "aws_s3_bucket.a", "type": "aws_s3_bucket", "action": "update",
             "body": "~ tags", "changedAttrs": ["versioning.0.enabled"], "isNew": True},
            {"address": "aws_s3_bucket.b", "type": "aws_s3_bucket", "action": "update",
             "body": "~ tags", "changedAttrs": ["tags.Owner"], "ignored": "tagging service"},
        ],
        "driftedResources": [{"address": "aws_sg.x", "type": "aws_sg", "actions": ["update"],
                              "isNew": False}],
        "planDiagram": DIAGRAM, "tags": {"Owner": "a"}, "defaultTags": {},
        "inputs": [], "outputs": [], "providers": [], "tfVersion": "",
        "moduleSource": "git::https://example.test/mod.git?ref=v1", "resourceCount": 4,
        "stateAgeDays": 12, "duration": 33.0, "owners": ["@org/prod"],
        "trend": "new", "prevStatus": "clean", "since": "2026-11-03", "ignoredCount": 1,
        "throttleRetries": 1,
    }
    base = {k: drift[k] for k in ("inputs", "outputs", "providers", "defaultTags")}

    def unit(rel, status, **kw):
        segs = (kw.pop("display", None) or rel).split("/")
        e = dict(base, unit=rel, displayUnit="/".join(segs), environment=segs[0],
                 subscription=segs[1] if len(segs) > 1 else "",
                 region=segs[2] if len(segs) > 2 else "", application="/".join(segs[3:]),
                 status=status, isStack=False, stackName="", summary="", diff="", error="",
                 errorClass="", planResources=[], driftedResources=[], planDiagram="",
                 tags=None, tfVersion="", moduleSource="", resourceCount=1,
                 stateAgeDays=None, duration=5.0, owners=[], trend="", prevStatus="", since="")
        e.update(kw)
        return e

    return [
        drift,
        unit("prod/sub1/eu-west-1/db", "error", error="Error: AccessDenied",
             errorClass="auth", trend="persisting", since="2026-10-20", owners=["@org/dba"]),
        unit("prod/sub1/eu-west-1/slow", "timeout", error="TIMEOUT", errorClass="timeout"),
        unit("dev/sub2/eu-west-1/web", "clean", trend="resolved", prevStatus="drift"),
        unit("dev/sub2/eu-west-1/asg", "clean", ignoredCount=1, ignoredStatus="drift"),
        unit("stk/live/.terragrunt-stack/services/api", "drift", display="stk/live/services/api",
             isStack=True, stackName="stk/live", summary="Plan: 1 to add, 0 to change, 0 to destroy.",
             planResources=[{"address": "aws_lambda_function.f", "type": "aws_lambda_function",
                             "action": "create", "body": "+ f"}], trend="new"),
        unit("stk/live/.terragrunt-stack/services/queue", "clean", display="stk/live/services/queue",
             isStack=True, stackName="stk/live"),
    ]


META = {"trend": {"baseline": "terrahawk_20261102_060000.json", "baselineDate": "2026-11-02",
                  "counts": {"new": 2, "persisting": 1, "resolved": 1, "newDrift": 2, "newError": 0}}}
STACKS = [{"name": "stk/live", "root": "stk/live", "unitCount": 2,
           "mermaid": "graph LR\n  s0[\"services/queue\"] --> s1[\"services/api\"]"}]


@pytest.fixture(scope="module")
def report_url(tmp_path_factory):
    out = tmp_path_factory.mktemp("report") / "terrahawk_20261103_060000.html"
    args = SimpleNamespace(diagrams=True, tags=True, diagram_assets="sidecar")
    report.generate_report(_fixture_data(), str(out), "2026-11-03 06:00:00",
                           {"terraform": "1.17.1", "terragrunt": "1.2.1"}, args, STACKS, META)
    return out.as_uri()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser not installed
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(10000)  # fail fast when the report breaks
    # Offline: the report must not depend on the network (web fonts fall back).
    pg.route("http*://**", lambda route: route.abort())
    errors = []
    pg.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" and "ERR_FAILED" not in m.text and "net::" not in m.text else None)
    pg.errors = errors
    yield pg
    ctx.close()


@pytest.mark.parametrize("theme", THEMES)
def test_renders_cleanly_per_theme(page, report_url, theme):
    page.goto(f"{report_url}?theme={theme}")
    page.wait_for_selector("#results .unit-row")
    assert page.get_attribute("html", "data-theme") == theme

    # Every unit renders: flat rows + stack leaves (stack header rows excluded).
    n_units = len(_fixture_data())
    assert page.locator("#results .unit-row").count() >= n_units
    assert page.locator("#stats .stat-card").count() == 5          # incl. timeouts
    assert page.locator("#err-classes .err-chip").count() == 2      # auth + timeout

    # Run-over-run bar + badges
    assert page.locator("#trend-bar .trend-chip").count() == 3
    assert page.locator(".trend-badge.trend-new").count() == 2
    assert page.locator(".trend-badge.trend-resolved").count() == 1
    assert page.locator(".owner-chip").count() == 2
    assert page.locator(".ign-badge").count() == 2
    assert page.locator("#owner-filter").is_visible()

    # Theme actually applied: body colours come from the theme tokens.
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg and bg != "rgba(0, 0, 0, 0)"

    assert page.errors == []


def test_themes_are_visually_distinct(page, report_url):
    colours = set()
    for theme in THEMES:
        page.goto(f"{report_url}?theme={theme}")
        page.wait_for_selector("#results .unit-row")
        colours.add(page.evaluate("getComputedStyle(document.body).backgroundColor"))
    assert len(colours) == len(THEMES)
    assert page.errors == []


def test_unknown_theme_falls_back(page, report_url):
    page.goto(f"{report_url}?theme=nope")
    page.wait_for_selector("#results .unit-row")
    assert page.get_attribute("html", "data-theme") == "dark"
    assert page.errors == []


def test_trend_and_owner_filters(page, report_url):
    page.goto(report_url)
    page.wait_for_selector("#results .unit-row")
    page.click("#trend-bar .trend-chip[data-trend='new']")
    rows = page.locator("#results .unit-row.status-drift")
    assert rows.count() == 2
    assert page.locator("#results .unit-row.status-clean").count() == 0
    page.click("#trend-bar .trend-chip[data-trend='new']")           # toggle off
    page.select_option("#owner-filter", "@org/dba")
    assert page.locator("#results .unit-row.status-error").count() == 1
    assert page.locator("#results .unit-row.status-drift").count() == 0
    assert page.errors == []


@pytest.mark.parametrize("theme", ("dark", "light"))
def test_plan_popup_and_diagram(page, report_url, theme):
    page.goto(f"{report_url}?theme={theme}")
    page.wait_for_selector("#results .unit-row")
    page.locator(".act-plan").first.click()
    page.wait_for_selector("#info-modal.open #plan-table-wrap table")
    table = page.inner_text("#plan-table-wrap")
    assert "aws_s3_bucket.a" in table and "new" in table and "ignored" in table
    page.click("#info-modal .modal-close")

    page.locator(".act-diag").first.click()
    page.wait_for_selector("#diagram-modal.open #diagram-canvas svg", timeout=15000)
    assert page.locator("#diagram-canvas pre").count() == 0          # no Mermaid error
    page.click("#diagram-modal .modal-close")

    page.locator(".stack-chip").first.click()
    page.wait_for_selector("#diagram-modal.open #diagram-canvas svg", timeout=15000)
    assert page.errors == []
