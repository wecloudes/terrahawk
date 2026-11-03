"""Unit detail view: formatting, wrapping and drawing."""

import curses
import json

from .common import ACTION_COLORS, STATUS_COLORS, _enc_suffix, _safe


# ── Diff coloring ─────────────────────────────────────────────────

def _diff_line_color(line):
    s = line.lstrip()
    if s.startswith("#"):
        if "will be created" in s: return 1
        if "will be destroyed" in s: return 3
        if "must be replaced" in s: return 4
        if "will be updated in-place" in s: return 2
        if "will be read" in s: return 5
        return 7
    if s.startswith("+/- ") or s.startswith("-/+ "): return 4
    if s.startswith("+ ") or (s.startswith("+") and not s.startswith("+/")): return 1
    if s.startswith("- ") or (s.startswith("-") and not s.startswith("-/")): return 3
    if s.startswith("~ "): return 2
    if s.startswith("<= "): return 5
    return 7


# ── Detail view ───────────────────────────────────────────────────

def _format_detail(unit):
    lines = []
    status = unit.get("status", "unknown")
    color = STATUS_COLORS.get(status, 7)

    lines.append((5, f" Unit: {unit['unit']}"))
    lines.append((color, f" Status: {status.upper()}"))
    trend = unit.get("trend")
    if trend == "new":
        was = f" (was {unit['prevStatus']})" if unit.get("prevStatus") else ""
        lines.append((3, f" Since last run: NEW{was}"))
    elif trend == "persisting":
        lines.append((7, f" Since last run: persisting since {unit.get('since') or '?'}"))
    elif trend == "resolved":
        lines.append((1, f" Since last run: resolved (was {unit.get('prevStatus', '?')})"))
    if unit.get("owners"):
        lines.append((5, f" Owners: {' '.join(unit['owners'])}"))
    if unit.get("ignoredCount"):
        extra = f" — would be {unit['ignoredStatus'].upper()}" if unit.get("ignoredStatus") else ""
        lines.append((7, f" Ignored: {unit['ignoredCount']} change(s) by .terrahawkignore{extra}"))
    if unit.get("throttleRetries"):
        lines.append((7, f" Rate limited: re-planned {unit['throttleRetries']}x after provider throttling"))
    if unit.get("isStack"):
        sn = unit.get("stackName") or ""
        lines.append((5, f" Stack: {sn} (from terragrunt.stack.hcl)" if sn else " Stack: (from terragrunt.stack.hcl)"))
    lines.append((7, ""))

    if unit.get("moduleSource"):
        lines.append((5, " Module Source"))
        lines.append((7, f"   {unit['moduleSource']}"))
        lines.append((7, ""))

    info_parts = []
    if unit.get("resourceCount"):
        info_parts.append(f"Resources: {unit['resourceCount']}")
    if unit.get("stateAgeDays") is not None:
        info_parts.append(f"State Age: {unit['stateAgeDays']}d")
    if unit.get("duration") is not None:
        info_parts.append(f"Duration: {unit['duration']:.0f}s")
    if unit.get("tfVersion"):
        info_parts.append(f"Terraform: {unit['tfVersion']}")
    if info_parts:
        lines.append((7, " " + "  |  ".join(info_parts)))
        lines.append((7, ""))

    # Providers
    providers = unit.get("providers", [])
    if providers:
        lines.append((5, " Providers"))
        for p in providers:
            ver = f" {p['version']}" if p.get("version") else ""
            src = f" ({p['source']})" if p.get("source") else ""
            lines.append((7, f"   {p['name']}{ver}{src}"))
        lines.append((7, ""))

    # HCL hygiene (validation issues + formatting)
    hcl_issues = unit.get("hclIssues") or []
    if hcl_issues:
        lines.append((3, f" ⚠ HCL Validation Issues ({len(hcl_issues)})"))
        for i in hcl_issues:
            lines.append((3, f"   {i.get('file', '?')}:{i.get('line', '?')}  {i.get('summary', '')}"))
        lines.append((7, ""))
    unfmt = unit.get("unformattedFiles") or []
    if unfmt:
        lines.append((2, f" ✎ Needs Formatting ({len(unfmt)})"))
        for f in unfmt:
            lines.append((2, f"   {f}"))
        lines.append((7, ""))

    # Out-of-band drift (resources changed outside Terraform — any status)
    drifted = unit.get("driftedResources") or []
    if drifted:
        lines.append((2, f" ⚠ Changed Outside Terraform ({len(drifted)})"))
        for r in drifted:
            acts = ",".join(r.get("actions", []))
            lines.append((2, f"   {acts:8s}  {r.get('address', '?')}{_enc_suffix(r)}"))
        lines.append((7, ""))

    # Drift
    if status == "drift":
        if unit.get("summary"):
            lines.append((2, f" {unit['summary']}"))
            lines.append((7, ""))

        plan_resources = unit.get("planResources", [])
        if plan_resources:
            lines.append((5, " Resource Changes"))
            for r in plan_resources:
                ac = ACTION_COLORS.get(r.get("action", "?"), 7)
                lines.append((ac, f"   {r.get('action', '?'):8s}  {r.get('address', '?')}{_enc_suffix(r)}"))
            if any(r.get("encodingOnly") for r in plan_resources):
                lines.append((7, ""))
                lines.append((5, " gzip re-encode only = base64-gzip value changes but decodes to identical"))
                lines.append((5, " content (Go 1.27 base64gzip encoder); the planned action is still real."))
            lines.append((7, ""))
            lines.append((5, " Plan Diff"))
            for r in plan_resources:
                body = r.get("body", "")
                if not body:
                    continue
                lines.append((7, ""))
                for dl in body.splitlines():
                    lines.append((_diff_line_color(dl), f"   {dl}"))
        elif unit.get("diff"):
            lines.append((5, " Plan Diff"))
            for dl in unit["diff"].splitlines():
                lines.append((_diff_line_color(dl), f"   {dl}"))

    # Error
    if status in ("error", "timeout") and unit.get("error"):
        lines.append((5, " Error Output"))
        for el in unit["error"].splitlines():
            lines.append((3, f"   {el}"))
        lines.append((7, ""))

    # Outputs
    outputs = unit.get("outputs", {})
    if outputs:
        lines.append((5, " Outputs"))
        for k, v in outputs.items():
            val = json.dumps(v) if not isinstance(v, str) else v
            lines.append((7, f"   {k} = {val}"))
        lines.append((7, ""))

    # Tags
    tags = unit.get("tags")
    if tags:
        lines.append((5, " Tags"))
        for k, v in sorted(tags.items()):
            lines.append((7, f"   {k} = {v}"))
        lines.append((7, ""))

    # Inputs
    inputs = unit.get("inputs", [])
    if inputs:
        lines.append((5, " Input Variables"))
        for inp in inputs:
            name = inp.get("name", "?")
            tp = inp.get("type", "")
            dflt = inp.get("default", "")
            desc = inp.get("description", "")
            sig = f"   {name}"
            if tp:
                sig += f"  ({tp})"
            if "value" in inp:
                val = str(inp["value"])
                sig += f"  = {val[:60]}"
            elif dflt:
                sig += f"  = {dflt}"
            lines.append((5, sig))
            if desc:
                lines.append((7, f"     {desc}"))

    return lines


def _wrap_lines(lines, w):
    """Wrap lines to fit within width w at word boundaries, preserving color and indent."""
    out = []
    usable = max(w - 1, 10)

    # Characters where we prefer to break (after these chars)
    _BREAK_AFTER = set(' ,.:;=/)]}>"\'')

    def _find_break(text, limit):
        """Find the best position to break `text` at, up to `limit` chars.

        Prefers breaking after a space/punctuation. Falls back to hard cut.
        """
        if len(text) <= limit:
            return len(text)
        # Look backwards from limit for a good break point
        best = -1
        for i in range(min(limit, len(text)) - 1, max(limit // 3, 0) - 1, -1):
            if text[i] in _BREAK_AFTER:
                best = i + 1  # break after the delimiter
                break
        if best > 0:
            return best
        # No good break found — hard cut
        return limit

    for color, text in lines:
        if len(text) <= usable:
            out.append((color, text))
            continue

        # Detect leading whitespace for continuation indent
        stripped = text.lstrip()
        indent = len(text) - len(stripped)
        cont_indent = " " * min(indent + 4, usable // 2)

        # First line
        brk = _find_break(text, usable)
        out.append((color, text[:brk]))
        remaining = text[brk:]

        # Continuation lines
        avail = usable - len(cont_indent)
        while remaining:
            brk = _find_break(remaining, avail)
            out.append((color, cont_indent + remaining[:brk]))
            remaining = remaining[brk:]

    return out


def _draw_detail(stdscr, lines, scroll, hscroll, wrap):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    if wrap:
        display_lines = _wrap_lines(lines, w)
    else:
        display_lines = lines

    visible = display_lines[scroll:scroll + h - 1]
    for i, (color, text) in enumerate(visible):
        if i >= h - 1:
            break
        if wrap:
            display = text
        else:
            display = text[hscroll:] if hscroll < len(text) else ""
        _safe(stdscr, i, 0, display[:w - 1], w - 1, curses.color_pair(color))

    wrap_ind = "W" if wrap else " "
    footer = f" \u2191\u2193 scroll  \u2190\u2192 hscroll  0 reset  w wrap[{wrap_ind}]  p plan  m module  d diagram  D browser  Esc/q back"
    _safe(stdscr, h - 1, 0, footer[:w - 1], w - 1, curses.A_DIM)
    stdscr.refresh()
    return len(display_lines)
