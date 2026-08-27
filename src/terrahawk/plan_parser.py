"""Plan output parsing: extract structured resource changes from terraform plan text."""

import json
import re

_PLAN_ACTION_MAP = {
    "will be created":            "create",
    "will be destroyed":          "delete",
    "will be updated in-place":   "update",
    "must be replaced":           "replace",
    "will be read during apply":  "read",
}
_PLAN_ACTION_RE = re.compile(
    r'^\s*#\s+(.+?)\s+(will be created|will be destroyed|will be updated in-place|must be replaced|will be read during apply)\b'
)
_PLAN_DECL_RE = re.compile(r'^\s*[-+~/<=\s]+(?:resource|data)\s+"([^"]+)"')


_HEREDOC_OPEN_RE = re.compile(r'<<-?\s*([A-Za-z_]\w*)\s*$')


def _count_braces(line):
    """Count net brace depth change on a line, ignoring braces inside strings
    and `#` / `//` line comments."""
    depth = 0
    in_str = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and in_str and i + 1 < n:
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            # A line comment starts here — the rest of the line is not code.
            if c == "#":
                break
            if c == "/" and i + 1 < n and line[i + 1] == "/":
                break
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
        i += 1
    return depth


def parse_plan_resources(plan_output):
    """Extract a structured list of resource changes from `terraform plan` text.

    Each entry is {address, type, action, body} where `body` is the full
    resource block (comment + declaration + attribute lines + closing brace),
    so the frontend can render per-resource diffs with colouring.
    """
    resources = []
    lines = plan_output.splitlines()
    for idx, line in enumerate(lines):
        m = _PLAN_ACTION_RE.match(line)
        if not m:
            continue
        addr = m.group(1).strip()
        action = _PLAN_ACTION_MAP[m.group(2)]
        # Find the resource/data declaration (usually 1 line after, may be 2-3
        # when there's a "# (because ...)" continuation comment).
        decl_idx = None
        for k in range(idx + 1, min(idx + 6, len(lines))):
            if _PLAN_DECL_RE.match(lines[k]):
                decl_idx = k
                break
        if decl_idx is None:
            continue
        rtype = _PLAN_DECL_RE.match(lines[decl_idx]).group(1)
        # Walk forward, tracking brace depth until the block closes. Skip brace
        # counting inside heredoc bodies (`<<-EOT ... EOT`), whose free-text
        # content can contain unbalanced braces that would corrupt the depth.
        body_lines = list(lines[idx:decl_idx + 1])
        depth = _count_braces(lines[decl_idx])
        j = decl_idx + 1
        heredoc_tag = None
        while j < len(lines) and depth > 0:
            line = lines[j]
            body_lines.append(line)
            if heredoc_tag is not None:
                if line.strip() == heredoc_tag:
                    heredoc_tag = None
            else:
                hm = _HEREDOC_OPEN_RE.search(line)
                if hm:
                    heredoc_tag = hm.group(1)
                    depth += _count_braces(line[:hm.start()])
                else:
                    depth += _count_braces(line)
            j += 1
        resources.append({
            "address": addr,
            "type": rtype,
            "action": action,
            "body": "\n".join(body_lines).rstrip(),
        })
    return resources


# ---------------------------------------------------------------------------
# JSON plan parsing (terraform show -json <planfile>)
# ---------------------------------------------------------------------------

_JSON_ACTION_MAP = {
    ("create",):           "create",
    ("delete",):           "delete",
    ("update",):           "update",
    ("delete", "create"):  "replace",
    ("create", "delete"):  "replace",
    ("read",):             "read",
}


def _mask_sensitive(value, sensitive):
    """Replace values marked sensitive in the plan JSON with a placeholder."""
    if sensitive is True:
        return "(sensitive value)"
    if isinstance(value, dict) and isinstance(sensitive, dict):
        return {k: _mask_sensitive(v, sensitive.get(k, False)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sensitive, list):
        return [_mask_sensitive(v, sensitive[i] if i < len(sensitive) else False)
                for i, v in enumerate(value)]
    return value


_SECRET_KEY_RE = re.compile(
    r"password|passwd|secret|token|private_key|access_key|secret_key|"
    r"client_secret|api[_-]?key|credential|passphrase",
    re.IGNORECASE,
)


def _redact_by_keyname(obj):
    """Redact string leaf values whose key name strongly implies a secret.

    Heuristic backstop for state/outputs that lack provider `sensitive`
    markers: recursively walks dicts/lists and replaces a string value with
    "(redacted)" when its KEY matches a credential-like pattern. Keys and
    non-string values are left untouched (no structural changes).
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "(redacted)"
            else:
                out[k] = _redact_by_keyname(v)
        return out
    if isinstance(obj, list):
        return [_redact_by_keyname(v) for v in obj]
    return obj


def parse_plan_resources_json(plan_json):
    """Build the same {address, type, action, body} entries from plan JSON.

    Fallback for when text parsing yields nothing (wrapped lines, format
    changes). `body` is a compact before/after JSON dump with sensitive
    values masked.
    """
    resources = []
    for rc in (plan_json or {}).get("resource_changes", []):
        change = rc.get("change", {})
        actions = tuple(change.get("actions", []))
        action = _JSON_ACTION_MAP.get(actions)
        if action is None:  # no-op or unknown
            continue
        before = _redact_by_keyname(
            _mask_sensitive(change.get("before"), change.get("before_sensitive", False)))
        after = _redact_by_keyname(
            _mask_sensitive(change.get("after"), change.get("after_sensitive", False)))
        body_parts = [f"# {rc.get('address', '?')} ({action})"]
        if before is not None:
            body_parts.append("before: " + json.dumps(before, indent=2, default=str))
        if after is not None:
            body_parts.append("after: " + json.dumps(after, indent=2, default=str))
        resources.append({
            "address": rc.get("address", "?"),
            "type": rc.get("type", ""),
            "action": action,
            "body": "\n".join(body_parts),
        })
    return resources


def extract_resource_drift(plan_json):
    """Extract out-of-band drift (changes made outside Terraform) from plan JSON.

    Returns [{address, type, actions}] — values are intentionally omitted
    (drift entries may contain raw sensitive data in before/after).
    """
    drifted = []
    for rd in (plan_json or {}).get("resource_drift", []):
        actions = [a for a in rd.get("change", {}).get("actions", []) if a != "no-op"]
        if not actions:
            continue
        drifted.append({
            "address": rd.get("address", "?"),
            "type": rd.get("type", ""),
            "actions": actions,
        })
    return drifted
