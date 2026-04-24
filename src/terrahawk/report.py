"""HTML report generation."""

import json
import sys
from pathlib import Path


def get_html_template():
    """Return the HTML template. Loaded from the templates directory or companion file."""
    # Try loading from package templates directory first
    pkg_template = Path(__file__).parent / "templates" / "report.html"
    if pkg_template.exists():
        return pkg_template.read_text()
    # Fallback: try the old companion file location (for development)
    template_path = Path(__file__).parent / "terrahawk_template.html"
    if template_path.exists():
        return template_path.read_text()
    print("\u274c HTML template not found. Ensure templates/report.html exists.")
    sys.exit(1)


def generate_report(results, html_report, report_date, versions, args):
    """Generate the HTML report and a companion JS data file.

    The data file is a plain JS file that assigns the results array to
    ``window.TERRAHAWK_DATA``.  The HTML loads it via a ``<script src>`` tag,
    which works from ``file://``, S3, Azure Blob, GCS — anywhere static
    files can be served or opened directly.
    """
    html_path = Path(html_report)
    data_js_name = html_path.stem + "_data.js"
    data_js_path = html_path.parent / data_js_name

    # Write the data as a JS file
    data_js_path.write_text(
        "window.TERRAHAWK_DATA=" + json.dumps(results, ensure_ascii=False) + ";\n"
    )

    # Generate the HTML referencing the data file
    template = get_html_template()
    template = template.replace("%%DATA_FILE%%", data_js_name)
    template = template.replace("%%REPORT_DATE%%", report_date)
    template = template.replace("%%HAS_DIAGRAMS%%", "true" if args.diagrams else "false")
    template = template.replace("%%HAS_TAGS%%", "true" if args.tags else "false")
    template = template.replace("%%VERSIONS%%", json.dumps(versions))
    html_path.write_text(template)
