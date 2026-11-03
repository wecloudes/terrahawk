"""Slack / Microsoft Teams webhook summary after a scan.

The webhook URL is a credential (anyone holding it can post to the channel),
so it is read ONLY from the environment — `TERRAHAWK_WEBHOOK_URL`, injected
by the CI secret store — never from `.terrahawk.yml` or a CLI flag (both end
up in repos, logs or process listings). It is never printed.

- `TERRAHAWK_WEBHOOK_KIND`: `slack` | `teams` (default: detected from the
  URL host; Slack for hooks.slack.com, Teams otherwise)
- `notify_on` / `--notify-on`: `new` (default — only when new drift or new
  errors appeared since the baseline), `findings` (any drift/error), `never`
- `report_url` / `--report-url`: where the published report lives, linked
  from the message (not a secret)

Teams payloads are Adaptive Cards, the format accepted by Teams Workflows
("Post to a channel when a webhook request is received"), which replaced the
retired Office 365 connectors. Failures warn and never fail the scan.
Stdlib only.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

ENV_URL = "TERRAHAWK_WEBHOOK_URL"
ENV_KIND = "TERRAHAWK_WEBHOOK_KIND"
_MAX_LISTED = 10


def _kind(url, explicit):
    if explicit in ("slack", "teams"):
        return explicit
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return "slack" if host.endswith("slack.com") else "teams"


def _status_counts(results):
    c = {"total": len(results), "clean": 0, "drift": 0, "error": 0, "timeout": 0}
    for e in results:
        c[e.get("status", "error")] = c.get(e.get("status", "error"), 0) + 1
    return c


def should_notify(results, mode):
    if mode == "never":
        return False
    if mode == "findings":
        return any(e.get("status") in ("drift", "error", "timeout") for e in results)
    return any(e.get("trend") == "new" for e in results)


def _listed(results, mode):
    if mode == "findings":
        rows = [e for e in results if e.get("status") in ("drift", "error", "timeout")]
    else:
        rows = [e for e in results if e.get("trend") == "new"]
    rows.sort(key=lambda e: (e.get("status") != "drift", e.get("unit", "")))
    return rows


def _line(e):
    label = {"drift": "drift", "error": "error", "timeout": "timeout"}.get(e.get("status"), e.get("status"))
    n = len([r for r in (e.get("planResources") or []) if not r.get("ignored")])
    detail = f"{n} change(s)" if e.get("status") == "drift" and n else (e.get("errorClass") or "")
    owners = " ".join(e.get("owners") or [])
    parts = [f"{e.get('displayUnit') or e.get('unit')}", label]
    if detail:
        parts.append(detail)
    if owners:
        parts.append(owners)
    return " — ".join(parts)


def build_payload(kind, results, counts, mode, report_url="", baseline_label=""):
    c = _status_counts(results)
    rows = _listed(results, mode)
    title = ("Terrahawk: new drift/errors since last run" if mode != "findings"
             else "Terrahawk: drift/errors found")
    summary = (f"{c['total']} units · {c['drift']} drift · {c['error'] + c['timeout']} error"
               f" · new: {counts.get('newDrift', 0)} drift / {counts.get('newError', 0)} error"
               f" · resolved: {counts.get('resolved', 0)}")
    if baseline_label:
        summary += f" (vs {baseline_label})"
    lines = [_line(e) for e in rows[:_MAX_LISTED]]
    if len(rows) > _MAX_LISTED:
        lines.append(f"…and {len(rows) - _MAX_LISTED} more")

    if kind == "slack":
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        ]
        if lines:
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": "\n".join(f"• `{l}`" for l in lines)}})
        if report_url:
            blocks.append({"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "Open report"},
                "url": report_url}]})
        return {"text": f"{title}: {summary}", "blocks": blocks}

    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": summary, "wrap": True, "isSubtle": True},
    ]
    for l in lines:
        body.append({"type": "TextBlock", "text": f"- {l}", "wrap": True, "spacing": "Small"})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": body}
    if report_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open report", "url": report_url}]
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def report_link(report_url, html_name):
    """`report_url` may be the report itself or a base folder (ending in /)."""
    if not report_url:
        return ""
    return report_url + html_name if report_url.endswith("/") else report_url


def maybe_notify(results, counts, mode, report_url="", html_name="", baseline_label="",
                 env=None):
    """Post the summary when configured and triggered. Returns True if sent."""
    env = os.environ if env is None else env
    url = (env.get(ENV_URL) or "").strip()
    if not url or mode == "never":
        return False
    if not url.startswith("https://"):
        print(f"  ⚠️  {ENV_URL} is not an https URL; not sending the alert.")
        return False
    if not should_notify(results, mode):
        print("\U0001f514 Alert: nothing new since the baseline, not sent.")
        return False
    kind = _kind(url, (env.get(ENV_KIND) or "").strip().lower())
    payload = build_payload(kind, results, counts, mode,
                            report_link(report_url, html_name), baseline_label)
    try:
        _post(url, payload)
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  {kind} webhook rejected the alert (HTTP {e.code}).")
        return False
    except Exception as e:  # network/DNS/TLS — the URL itself is never printed
        print(f"  ⚠️  Could not send the {kind} alert ({type(e).__name__}).")
        return False
    print(f"\U0001f514 Sent {kind} alert.")
    return True
