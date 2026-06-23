"""Push a generated report to a Terrakettle server.

Standard-library only (urllib + multipart hand-rolled), consistent with the
rest of Terrahawk, so it runs inside the Docker images without extra deps.
"""

import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _multipart(fields, files):
    """Build a multipart/form-data body. files: list of (field, path, ctype)."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    for name, path, ctype in files:
        data = Path(path).read_bytes()
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{name}"; '
                 f'filename="{Path(path).name}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def push_report(url, token, json_path, html_path=None, data_js_path=None,
                run_id=None, timeout=60):
    """Upload a report triple to ``{url}/api/v1/runs``.

    Returns the decoded response body on success, raises on HTTP error.
    """
    json_path = Path(json_path)
    rid = run_id or json_path.stem
    files = [("report", str(json_path), "application/json")]
    if html_path and Path(html_path).exists():
        files.append(("html", str(html_path), "text/html"))
    if data_js_path and Path(data_js_path).exists():
        files.append(("data_js", str(data_js_path), "application/javascript"))

    ctype, body = _multipart({"run_id": rid}, files)
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/runs", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": ctype},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def maybe_push(args, json_report, html_report):
    """Push the report if ``--push-url`` (and a token) are configured.

    Prints the outcome; never raises so a publish failure does not fail the
    whole scan. Derives the data.js sidecar path from the HTML report name.
    """
    url = getattr(args, "push_url", None)
    if not url:
        return
    token = getattr(args, "push_token", None) or os.environ.get("TERRAKETTLE_TOKEN")
    if not token:
        print("⚠️  --push-url set but no token "
              "(--push-token or $TERRAKETTLE_TOKEN); skipping push")
        return

    html_path = Path(html_report)
    data_js_path = html_path.with_name(html_path.stem + "_data.js")
    print(f"\U0001f4e4 Pushing report to {url} ...")
    try:
        resp = push_report(url, token, json_report, html_report, data_js_path)
        print(f"  ✅ Pushed: {resp}")
    except urllib.error.HTTPError as e:
        print(f"  ❌ Push failed: {e.code} {e.read().decode(errors='replace')}")
    except Exception as e:  # network, timeout, etc. — non-fatal
        print(f"  ❌ Push failed: {e}")
