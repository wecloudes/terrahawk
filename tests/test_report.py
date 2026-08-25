"""Unit tests for terrahawk.report HTML generation."""

import re
import types
from pathlib import Path

import pytest

from terrahawk import report

_VENDOR_JS = Path(report.__file__).parent / "templates" / "vendor" / "mermaid.min.js"


class TestGetMermaidScript:
    def test_inlines_vendored_library(self):
        if not _VENDOR_JS.exists():
            pytest.skip("vendored mermaid.min.js not bundled")
        script = report.get_mermaid_script()
        assert script.startswith("<script>")
        assert script.rstrip().endswith("</script>")
        # air-gapped: no CDN reference when vendored copy is inlined
        assert "jsdelivr" not in script
        # some recognisable slice of the library body is present
        assert "mermaid" in script.lower()

    def test_escapes_closing_script_tag(self):
        if not _VENDOR_JS.exists():
            pytest.skip("vendored mermaid.min.js not bundled")
        script = report.get_mermaid_script()
        body = script[len("<script>"):script.rstrip().rfind("</script>")]
        # the inlined body must not contain a raw </script> that would close early
        assert "</script>" not in body


class TestGenerateReport:
    def _make_args(self):
        return types.SimpleNamespace(diagrams=False, tags=False)

    def test_end_to_end_writes_html_and_sidecar(self, tmp_path):
        results = [
            {"path": "dev/vpc", "status": "clean"},
            {"path": "dev/app", "status": "drift"},
        ]
        html_report = tmp_path / "report.html"
        report.generate_report(
            results,
            str(html_report),
            report_date="2026-08-25",
            versions={"terrahawk": "1.5.0"},
            args=self._make_args(),
        )

        # sidecar _data.js written next to the html
        data_js = tmp_path / "report_data.js"
        assert data_js.exists()
        data_body = data_js.read_text()
        assert "window.TERRAHAWK_DATA=" in data_body
        assert "window.TERRAHAWK_STACKS=" in data_body

        html = html_report.read_text()
        # no unresolved template placeholders remain
        assert not re.search(r"%%[A-Z_]+%%", html)
        # the report references its data sidecar
        assert "report_data.js" in html

    def test_inline_mode_is_air_gapped(self, tmp_path):
        if not _VENDOR_JS.exists():
            pytest.skip("vendored mermaid.min.js not bundled")
        html_report = tmp_path / "report.html"
        report.generate_report(
            [],
            str(html_report),
            report_date="2026-08-25",
            versions={},
            args=self._make_args(),
        )
        html = html_report.read_text()
        assert "jsdelivr" not in html
