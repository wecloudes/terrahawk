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


def generate_report(results, html_report, report_date, versions, args, stack_graphs=None):
    """Generate the HTML report and a companion JS data file.

    The data file is a plain JS file that assigns the results array to
    ``window.TERRAHAWK_DATA`` (and the per-stack diagrams to
    ``window.TERRAHAWK_STACKS``).  The HTML loads it via a ``<script src>`` tag,
    which works from ``file://``, S3, Azure Blob, GCS — anywhere static
    files can be served or opened directly.
    """
    html_path = Path(html_report)
    data_js_name = html_path.stem + "_data.js"
    data_js_path = html_path.parent / data_js_name

    # Write the data as a JS file
    data_js_path.write_text(
        "window.TERRAHAWK_DATA=" + json.dumps(results, ensure_ascii=False) + ";\n"
        "window.TERRAHAWK_STACKS=" + json.dumps(stack_graphs or [], ensure_ascii=False) + ";\n"
    )

    # Diagram runtime delivery mode: "inline" (default; self-contained but
    # bloats every report by ~3.5MB) or "sidecar" (write mermaid.min.js once
    # next to the report and reference it relatively). Default defensively so
    # older callers that don't set the attribute keep the inline behavior.
    diagram_assets = getattr(args, "diagram_assets", "inline")

    # Generate the HTML referencing the data file
    template = get_html_template()
    template = template.replace("%%DATA_FILE%%", data_js_name)
    template = template.replace("%%REPORT_DATE%%", report_date)
    template = template.replace("%%HAS_DIAGRAMS%%", "true" if args.diagrams else "false")
    template = template.replace("%%HAS_TAGS%%", "true" if args.tags else "false")
    template = template.replace("%%VERSIONS%%", json.dumps(versions))
    # Resolve the Mermaid runtime last, after every other placeholder is
    # resolved, so the minified library body (which may itself contain '%%'
    # sequences) can never clobber a template token.
    template = template.replace(
        "%%MERMAID_SCRIPT%%",
        get_mermaid_script(mode=diagram_assets, output_dir=html_path.parent),
    )
    html_path.write_text(template)


def get_mermaid_script(mode="inline", output_dir=None):
    """Return the ``<script>`` element that provides the Mermaid runtime.

    Prefers the vendored copy bundled with the package (and baked into the
    Docker images), producing a self-contained, air-gapped report. Falls back
    to the public CDN only when the vendored asset is unavailable.

    Two delivery modes are supported:

    - ``"inline"`` (default): the minified library body is embedded directly in
      a ``<script>`` element. Fully self-contained, but adds ~3.5MB to every
      report.
    - ``"sidecar"``: the vendored ``mermaid.min.js`` is written once into
      ``output_dir`` (deduped across reports sharing that directory) and
      referenced with a relative ``<script src>``. Still fully offline /
      air-gapped because the file travels with the report, exactly like the
      ``_data.js`` sidecar.
    """
    vendor_js = Path(__file__).parent / "templates" / "vendor" / "mermaid.min.js"
    if not vendor_js.exists():
        # No vendored asset: fall back to the public CDN regardless of mode.
        return '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'
    if mode == "sidecar":
        target_dir = Path(output_dir) if output_dir is not None else vendor_js.parent
        sidecar = target_dir / "mermaid.min.js"
        # Dedupe: only copy if a sidecar isn't already present in this dir.
        if not sidecar.exists():
            sidecar.write_bytes(vendor_js.read_bytes())
        return '<script src="mermaid.min.js"></script>'
    # Inline (default). Escape any literal ``</script>`` so the inlined library
    # body cannot terminate the surrounding <script> element prematurely.
    js = vendor_js.read_text().replace("</script>", "<\\/script>")
    return "<script>\n" + js + "\n</script>"
