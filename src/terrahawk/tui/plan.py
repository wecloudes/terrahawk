"""Plan view: per-resource change rows with action/type filters."""

import curses

from .common import ACTION_COLORS, ACTION_LABELS, ACTION_ORDER, _ALL_ACTIONS, _enc_suffix, _safe
from .detail import _diff_line_color, _wrap_lines


# ── Plan view ─────────────────────────────────────────────────────

def _build_plan_rows(plan_resources, action_filter, type_filter):
    resources = list(plan_resources or [])
    if action_filter:
        resources = [r for r in resources if r.get("action") == action_filter]
    if type_filter:
        resources = [r for r in resources if r.get("type") == type_filter]
    resources.sort(key=lambda r: (
        ACTION_ORDER.get(r.get("action", ""), 99),
        r.get("type", ""), r.get("address", ""),
    ))
    return [{"resource": r, "expanded": False} for r in resources]


def _get_plan_types(plan_resources):
    types = set()
    for r in (plan_resources or []):
        t = r.get("type", "")
        if t:
            types.add(t)
    return sorted(types)


def _draw_plan(stdscr, unit, plan_rows, cursor, scroll, action_filter, type_filter, hscroll, wrap):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    all_res = unit.get("planResources", [])

    header = f" Plan Details \u2014 {unit.get('unit', '')}"
    _safe(stdscr, 0, 0, header[:w - 1], w - 1, curses.color_pair(5) | curses.A_BOLD)

    # Badges
    counts = {}
    for r in all_res:
        counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
    x = 1
    for a in _ALL_ACTIONS:
        c = counts.get(a, 0)
        if c == 0:
            continue
        label = f" {c} {ACTION_LABELS.get(a, a)} "
        color = ACTION_COLORS.get(a, 7)
        attr = curses.color_pair(color) | curses.A_BOLD
        if action_filter == a:
            attr |= curses.A_REVERSE
        _safe(stdscr, 1, x, label[:w - x - 1], w - x - 1, attr)
        x += len(label) + 1

    # Filter line
    fparts = []
    if action_filter:
        fparts.append(f"action:{action_filter}")
    if type_filter:
        fparts.append(f"type:{type_filter}")
    if fparts:
        filt = " [" + "  ".join(fparts) + "]"
        _safe(stdscr, 2, 0, filt, w - 1, curses.A_DIM)

    # Build display lines
    display_lines = []
    for ri, pr in enumerate(plan_rows):
        r = pr["resource"]
        ac = ACTION_COLORS.get(r.get("action", "?"), 7)
        tag = ACTION_LABELS.get(r.get("action"), r.get("action", "?"))
        arrow = "\u25BC" if pr["expanded"] else "\u25B6"
        has_body = bool(r.get("body"))
        prefix = f" {arrow} " if has_body else "   "
        line = f"{prefix}{tag:12s} {r.get('type', ''):30s} {r.get('address', '?')}{_enc_suffix(r)}"
        display_lines.append((ri, "header", line, ac))
        if pr["expanded"] and r.get("body"):
            for bl in r["body"].splitlines():
                display_lines.append((ri, "body", f"      {bl}", _diff_line_color(bl)))

    # Wrap body lines if enabled (headers stay unwrapped for cursor mapping)
    if wrap:
        wrapped = []
        for ri, ltype, text, color in display_lines:
            if ltype == "body" and len(text) > w - 1:
                for wc, wt in _wrap_lines([(color, text)], w):
                    wrapped.append((ri, "body", wt, wc))
            else:
                wrapped.append((ri, ltype, text, color))
        render_lines = wrapped
    else:
        render_lines = display_lines

    list_start = 3
    list_h = h - list_start - 1
    visible = render_lines[scroll:scroll + list_h]

    for i, (ri, ltype, text, color) in enumerate(visible):
        y = list_start + i
        if y >= h - 1:
            break
        if wrap:
            disp = text
        else:
            disp = text[hscroll:] if hscroll < len(text) else ""
        if scroll + i == cursor:
            attr = curses.color_pair(6) | curses.A_BOLD
        else:
            attr = curses.color_pair(color)
        _safe(stdscr, y, 0, disp[:w - 1], w - 1, attr)

    wrap_ind = "W" if wrap else " "
    footer = f" \u2191\u2193 navigate  Enter expand  \u2190\u2192 hscroll  0 reset  w wrap[{wrap_ind}]  f action  t type  d diagram  D browser  Esc/q back"
    _safe(stdscr, h - 1, 0, footer[:w - 1], w - 1, curses.A_DIM)
    stdscr.refresh()
    return render_lines
