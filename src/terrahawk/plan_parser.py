"""Plan output parsing: extract structured resource changes from terraform plan text."""

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


def _count_braces(line):
    """Count net brace depth change on a line, ignoring braces inside strings."""
    depth = 0
    in_str = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and in_str and i + 1 < len(line):
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
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
        # Walk forward, tracking brace depth until the block closes.
        body_lines = list(lines[idx:decl_idx + 1])
        depth = _count_braces(lines[decl_idx])
        j = decl_idx + 1
        while j < len(lines) and depth > 0:
            body_lines.append(lines[j])
            depth += _count_braces(lines[j])
            j += 1
        resources.append({
            "address": addr,
            "type": rtype,
            "action": action,
            "body": "\n".join(body_lines).rstrip(),
        })
    return resources
