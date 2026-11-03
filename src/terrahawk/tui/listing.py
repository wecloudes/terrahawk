"""Unit list view: filtering, grouping rows, coverage bar and drawing."""

import curses

from .common import STATUS_COLORS, STATUS_ORDER, _safe


# ── Filtering & sorting ──────────────────────────────────────────

def _get_subscriptions(all_units):
    """Return sorted list of unique env/sub keys."""
    subs = set()
    for u in all_units:
        subs.add(u.get("environment", "") + "/" + u.get("subscription", ""))
    return sorted(subs)


def _get_tag_keys(all_units):
    """Return sorted list of unique tag keys across all units."""
    keys = set()
    for u in all_units:
        tags = u.get("tags")
        if tags:
            keys.update(tags.keys())
    return sorted(keys)


def _get_tag_values(all_units, key):
    """Return sorted list of unique values for a given tag key."""
    vals = set()
    for u in all_units:
        tags = u.get("tags")
        if tags and key in tags:
            vals.add(tags[key])
    return sorted(vals)


def _get_tag_completions(all_units):
    """Return sorted list of all tag key=value pairs for autocomplete."""
    pairs = set()
    keys = set()
    for u in all_units:
        tags = u.get("tags")
        if tags:
            for k, v in tags.items():
                keys.add(k)
                pairs.add(f"{k}={v}")
    return sorted(keys), sorted(pairs)


def _apply_filters(all_units, filter_status, filter_sub, filter_tag, search_query, sort_mode):
    units = list(all_units)
    if filter_status:
        units = [u for u in units if u.get("status") == filter_status]
    if filter_sub:
        units = [u for u in units if u.get("environment", "") + "/" + u.get("subscription", "") == filter_sub]
    if filter_tag:
        if "=" in filter_tag:
            tk, tv = filter_tag.split("=", 1)
            units = [u for u in units if u.get("tags", {}).get(tk) == tv]
        else:
            units = [u for u in units if filter_tag in u.get("tags", {})]
    if search_query:
        q = search_query.lower()
        units = [u for u in units if q in u.get("unit", "").lower()]

    if sort_mode == "name":
        units.sort(key=lambda u: u.get("unit", ""))
    elif sort_mode == "resources":
        units.sort(key=lambda u: (-(u.get("resourceCount") or 0), u.get("unit", "")))
    elif sort_mode == "duration":
        units.sort(key=lambda u: (-(u.get("duration") or 0), u.get("unit", "")))
    else:  # status
        units.sort(key=lambda u: (
            u.get("environment", "") + "/" + u.get("subscription", ""),
            STATUS_ORDER.get(u.get("status", ""), 99),
            u.get("unit", ""),
        ))
    return units


# ── Row building ──────────────────────────────────────────────────

def _build_rows(units):
    groups = {}
    for i, u in enumerate(units):
        key = u.get("environment", "") + "/" + u.get("subscription", "")
        if key not in groups:
            groups[key] = []
        groups[key].append((i, u))
    rows = []
    for gk in sorted(groups):
        group_units = [u for _, u in groups[gk]]
        rows.append(("header", gk, group_units))
        for idx, u in groups[gk]:
            rows.append(("unit", u, idx))
    return rows


def _snap_cursor(rows, cursor, direction=1):
    if not rows:
        return 0
    cursor = max(0, min(cursor, len(rows) - 1))
    for d in (direction, -direction):
        pos = cursor
        while 0 <= pos < len(rows):
            if rows[pos][0] == "unit":
                return pos
            pos += d
    for i, r in enumerate(rows):
        if r[0] == "unit":
            return i
    return 0


# ── Coverage bar ──────────────────────────────────────────────────

def _draw_coverage_bar(stdscr, y, w, all_units):
    total = len(all_units)
    if total == 0:
        return
    counts = {}
    for u in all_units:
        s = u.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    bar_w = max(w - 4, 10)
    clean_n = counts.get("clean", 0)
    drift_n = counts.get("drift", 0)
    error_n = counts.get("error", 0) + counts.get("timeout", 0)

    clean_w = round(clean_n / total * bar_w)
    drift_w = round(drift_n / total * bar_w)
    error_w = bar_w - clean_w - drift_w

    x = 2
    if clean_w > 0:
        _safe(stdscr, y, x, "\u2588" * clean_w, clean_w, curses.color_pair(1))
        x += clean_w
    if drift_w > 0:
        _safe(stdscr, y, x, "\u2588" * drift_w, drift_w, curses.color_pair(2))
        x += drift_w
    if error_w > 0:
        _safe(stdscr, y, x, "\u2588" * error_w, error_w, curses.color_pair(3))
        x += error_w

    pct = round(clean_n / total * 100)
    label = f" {pct}% clean"
    _safe(stdscr, y, x + 1, label, w - x - 2, curses.A_DIM)


# ── List view draw ────────────────────────────────────────────────

def _draw_list(stdscr, rows, cursor, scroll, filters, all_units, sort_mode):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    filter_status, filter_sub, filter_tag, search_query = filters
    total = len(all_units)
    counts = {}
    for u in all_units:
        s = u.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    # Line 0: header
    header = f" Terrahawk  |  {total} units"
    parts = []
    for s in ("clean", "drift", "error", "timeout"):
        c = counts.get(s, 0)
        if c > 0:
            parts.append(f"{s}: {c}")
    if parts:
        header += "  |  " + "  ".join(parts)
    _safe(stdscr, 0, 0, header, w - 1, curses.color_pair(5) | curses.A_BOLD)

    # Line 1: coverage bar
    _draw_coverage_bar(stdscr, 1, w, all_units)

    # Line 2: active filters
    bar_parts = []
    if filter_status:
        bar_parts.append(f"status:{filter_status}")
    if filter_sub:
        bar_parts.append(f"sub:{filter_sub}")
    if filter_tag:
        bar_parts.append(f"tag:{filter_tag}")
    if search_query:
        bar_parts.append(f"search:{search_query}")
    bar_parts.append(f"sort:{sort_mode}")
    bar = " [" + "  ".join(bar_parts) + "]"
    _safe(stdscr, 2, 0, bar, w - 1, curses.color_pair(5))

    # Rows start at line 3
    list_start = 3
    list_h = h - list_start - 1
    visible = rows[scroll:scroll + list_h]

    for i, row in enumerate(visible):
        y = list_start + i
        if y >= h - 1:
            break
        row_idx = scroll + i

        if row[0] == "header":
            _, gk, gus = row
            sep = " " + "\u2500" * max(w - 3, 0)
            _safe(stdscr, y, 0, sep, w - 1, curses.A_DIM)
            _safe(stdscr, y, 0, f" {gk}", w - 1, curses.color_pair(5) | curses.A_BOLD)
            # badges
            gc = {}
            tr = 0
            for u in gus:
                s = u.get("status", "unknown")
                gc[s] = gc.get(s, 0) + 1
                tr += u.get("resourceCount") or 0
            bx = len(gk) + 3
            for s in ("clean", "drift", "error", "timeout"):
                c = gc.get(s, 0)
                if c > 0 and bx < w - 1:
                    lbl = f" {c} {s} "
                    _safe(stdscr, y, bx, lbl, min(len(lbl), w - bx - 1),
                          curses.color_pair(STATUS_COLORS.get(s, 7)) | curses.A_BOLD)
                    bx += len(lbl) + 1
            info = f"{len(gus)} units  {tr} res"
            ix = max(bx + 2, w - len(info) - 2)
            if ix < w - 1:
                _safe(stdscr, y, ix, info, w - ix - 1, curses.A_DIM)

        elif row[0] == "unit":
            _, unit, _ = row
            status = unit.get("status", "?")
            res = str(unit.get("resourceCount", "")) if unit.get("resourceCount") else ""
            age = f"{unit['stateAgeDays']}d" if unit.get("stateAgeDays") is not None else ""
            summary = unit.get("summary", "")

            full_path = unit.get("displayUnit") or unit.get("unit", "")
            segs = full_path.split("/")
            short_path = "/".join(segs[2:]) if len(segs) > 2 else (segs[-1] if segs else full_path)

            sc = {"clean": "+", "drift": "~", "error": "x", "timeout": "!"}.get(status, "?")
            has_diag = "D" if unit.get("planDiagram") else " "
            has_plan = "P" if unit.get("planResources") else " "
            stk = "S" if unit.get("isStack") else " "
            trend = {"new": "N", "resolved": "R"}.get(unit.get("trend"), " ")
            # Warning flag: hcl issues / unformatted / out-of-band drift
            warn = "!" if (unit.get("hclIssues") or unit.get("unformattedFiles")
                           or unit.get("driftedResources")) else " "
            ntags = len(unit.get("tags") or {})
            tag_ind = f"T{ntags}" if ntags else "  "
            dur = f"{unit['duration']:.0f}s" if unit.get("duration") is not None else ""

            right = f" {trend}{stk}{warn}{has_plan}{has_diag} {tag_ind:>3s} {res:>5s} {age:>6s} {dur:>5s}"
            name_w = max(w - len(right) - 6, 10)
            if len(short_path) > name_w:
                short_path = "..." + short_path[-(name_w - 3):]

            row_text = f"   {sc} {short_path:{name_w}s}{right}"
            if summary:
                avail = w - len(row_text) - 2
                if avail > 10:
                    row_text += f"  {summary[:avail]}"

            if row_idx == cursor:
                attr = curses.color_pair(6) | curses.A_BOLD
            else:
                attr = curses.color_pair(STATUS_COLORS.get(status, 7))
            _safe(stdscr, y, 0, row_text[:w - 1], w - 1, attr)

    footer = " \u2191\u2193 navigate  Enter detail  p plan  m module  d diagram  D browser  f status  s sub  t tag  o sort  / search  c clear  q quit"
    _safe(stdscr, h - 1, 0, footer[:w - 1], w - 1, curses.A_DIM)
    stdscr.refresh()
