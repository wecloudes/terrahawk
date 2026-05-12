"""Terminal UI for viewing Terrahawk scan results."""

import curses
import json
import os
import re as _re
import subprocess
import sys
import tempfile
from pathlib import Path


# ── Constants ─────────────────────────────────────────────────────

STATUS_ORDER = {"drift": 0, "error": 1, "timeout": 2, "clean": 3}
STATUS_COLORS = {"clean": 1, "drift": 2, "error": 3, "timeout": 4}
ACTION_ORDER = {"create": 0, "replace": 1, "update": 2, "delete": 3, "read": 4}
ACTION_LABELS = {
    "create": "+ Add", "update": "~ Modify", "delete": "- Delete",
    "replace": "* Replace", "read": "? Read",
}
ACTION_COLORS = {"create": 1, "update": 2, "delete": 3, "replace": 4, "read": 5}
_ALL_ACTIONS = ("create", "replace", "update", "delete", "read")
SORT_MODES = ("status", "name", "resources")


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)     # clean / create
    curses.init_pair(2, curses.COLOR_YELLOW, -1)    # drift / update
    curses.init_pair(3, curses.COLOR_RED, -1)       # error / delete
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)   # timeout / replace
    curses.init_pair(5, curses.COLOR_CYAN, -1)      # info / header / read
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected
    curses.init_pair(7, curses.COLOR_WHITE, -1)     # normal


def _safe(stdscr, y, x, text, maxlen, attr):
    """Write text to screen, ignoring curses errors at boundaries."""
    try:
        stdscr.addnstr(y, x, text, maxlen, attr)
    except curses.error:
        pass


# ── Report loading ────────────────────────────────────────────────

def _find_report(name=None):
    results_dir = Path.cwd() / "terrahawk_results"
    if not results_dir.is_dir():
        print("\u274c No terrahawk_results/ directory found in current directory.")
        sys.exit(1)
    if name:
        for candidate in [Path(name), results_dir / name]:
            if candidate.is_file():
                return candidate
        if not name.endswith(".json"):
            candidate = results_dir / f"{name}.json"
            if candidate.is_file():
                return candidate
        matches = sorted(results_dir.glob(f"*{name}*.json"), key=os.path.getmtime, reverse=True)
        if matches:
            return matches[0]
        print(f"\u274c No report matching '{name}' found in {results_dir}")
        sys.exit(1)
    reports = sorted(results_dir.glob("terrahawk_*.json"), key=os.path.getmtime, reverse=True)
    if not reports:
        print(f"\u274c No terrahawk_*.json reports found in {results_dir}")
        sys.exit(1)
    return reports[0]


def _load_report(path):
    with open(path) as f:
        return json.load(f)


# ── Diagram ───────────────────────────────────────────────────────

_CLS_COLORS = {
    "addCls": 1, "updCls": 2, "delCls": 3, "repCls": 4,
    "readCls": 5, "stableCls": 7, "dataCls": 5,
}
_CLS_ICONS = {
    "addCls": "+", "updCls": "~", "delCls": "-",
    "repCls": "*", "readCls": "?", "stableCls": " ", "dataCls": "d",
}
_NODE_RE = _re.compile(r'^\s*(n\d+)\["(.+?)"\]:::(\w+)\s*$')
_EDGE_RE = _re.compile(r'^\s*(n\d+)\s*-->\s*(n\d+)\s*$')
_HTML_TAG_RE = _re.compile(r'</?(?:b|br|sub|span)[^>]*>(?:/)?')
_MERMAID_ESC = {"#34;": '"', "#91;": "[", "#93;": "]"}


def _clean_label(raw):
    text = _HTML_TAG_RE.sub(" ", raw)
    for esc, ch in _MERMAID_ESC.items():
        text = text.replace(esc, ch)
    return " ".join(text.split())


def _parse_diagram(src):
    nodes, edges, adj_out, adj_in = {}, [], {}, {}
    for line in src.splitlines():
        m = _NODE_RE.match(line)
        if m:
            nid, raw, cls = m.group(1), m.group(2), m.group(3)
            nodes[nid] = {"label": _clean_label(raw), "cls": cls}
            adj_out.setdefault(nid, [])
            adj_in.setdefault(nid, [])
            continue
        m = _EDGE_RE.match(line)
        if m:
            s, d = m.group(1), m.group(2)
            edges.append((s, d))
            adj_out.setdefault(s, []).append(d)
            adj_in.setdefault(d, []).append(s)
    return nodes, edges, adj_out, adj_in


def _build_diagram_lines(unit):
    diagram = unit.get("planDiagram", "")
    if not diagram:
        return []
    nodes, edges, adj_out, adj_in = _parse_diagram(diagram)
    if not nodes:
        return []

    lines = []
    lines.append((5, f" Architecture Diagram \u2014 {unit.get('unit', '')}"))
    lines.append((7, ""))

    cls_counts = {}
    for n in nodes.values():
        cls_counts[n["cls"]] = cls_counts.get(n["cls"], 0) + 1
    cls_labels = {
        "addCls": "create", "updCls": "update", "delCls": "delete",
        "repCls": "replace", "readCls": "read", "stableCls": "stable", "dataCls": "data",
    }
    parts = []
    for cls in ("addCls", "updCls", "delCls", "repCls", "readCls", "stableCls", "dataCls"):
        c = cls_counts.get(cls, 0)
        if c > 0:
            parts.append(f"{c} {cls_labels[cls]}")
    lines.append((7, f" {len(nodes)} resources, {len(edges)} dependencies"))
    if parts:
        lines.append((7, " " + "  ".join(parts)))
    lines.append((7, ""))

    changed = [nid for nid in sorted(nodes) if nodes[nid]["cls"] not in ("stableCls", "dataCls")]
    stable = [nid for nid in sorted(nodes) if nodes[nid]["cls"] in ("stableCls", "dataCls")]

    def render_group(title, nids, show_deps=True):
        if not nids:
            return
        lines.append((5, f" {title}"))
        for nid in nids:
            n = nodes[nid]
            lines.append((_CLS_COLORS.get(n["cls"], 7), f"   {_CLS_ICONS.get(n['cls'], ' ')} {n['label']}"))
            if show_deps:
                for dep_nid in adj_out.get(nid, []):
                    dep = nodes.get(dep_nid)
                    if dep:
                        lines.append((_CLS_COLORS.get(dep["cls"], 7), f"       \u2514\u2500\u2192 {dep['label']}"))
        lines.append((7, ""))

    render_group("Changed Resources", changed)
    render_group("Unchanged Resources", stable, show_deps=False)
    return lines


_DIAGRAM_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Terrahawk \u2014 {unit}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;overflow:hidden;height:100vh;display:flex;flex-direction:column}}
  .toolbar{{display:flex;align-items:center;gap:8px;padding:8px 16px;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}}
  .toolbar h3{{flex:1;font-size:14px;font-weight:600;color:#e6edf3}}
  .toolbar button{{background:#0d1117;border:1px solid #30363d;color:#8b949e;width:32px;height:32px;border-radius:6px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center}}
  .toolbar button:hover{{border-color:#8b949e;color:#e6edf3}}
  .zoom-label{{font-size:12px;color:#8b949e;min-width:40px;text-align:center;font-family:monospace}}
  .viewport{{flex:1;overflow:hidden;position:relative;cursor:grab}}
  .viewport.grabbing{{cursor:grabbing}}
  .canvas{{position:absolute;top:0;left:0;transform-origin:0 0}}
  .canvas svg{{display:block}}
</style>
</head><body>
<div class="toolbar">
  <h3>{unit}</h3>
  <button onclick="zoom(-0.2)" title="Zoom out">&minus;</button>
  <span class="zoom-label" id="zl">100%</span>
  <button onclick="zoom(0.2)" title="Zoom in">+</button>
  <button onclick="fit()" title="Fit to view">&square;</button>
  <button onclick="reset()" title="Reset">1:1</button>
</div>
<div class="viewport" id="vp"><div class="canvas" id="cv">
  <pre class="mermaid">{mermaid}</pre>
</div></div>
<script>
mermaid.initialize({{startOnLoad:true,theme:'dark'}});
let s=1,tx=0,ty=0,drag=false,sx=0,sy=0;
const vp=()=>document.getElementById('vp'),cv=()=>document.getElementById('cv');
function apply(){{cv().style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`;document.getElementById('zl').textContent=Math.round(s*100)+'%';}}
function zoom(d){{s=Math.max(0.1,Math.min(20,s+d));apply();}}
function reset(){{s=1;tx=0;ty=0;apply();}}
function fit(){{
  const c=cv(),v=vp();if(!c||!v)return;
  const svg=c.querySelector('svg');if(!svg)return;
  const vw=v.clientWidth,vh=v.clientHeight;
  let sw,sh;const vb=svg.getAttribute('viewBox');
  if(vb){{const p=vb.split(/[\\s,]+/);sw=parseFloat(p[2]);sh=parseFloat(p[3]);}}
  else{{sw=svg.scrollWidth;sh=svg.scrollHeight;}}
  if(!sw||!sh)return;
  s=Math.min(vw/sw,vh/sh)*0.9;tx=(vw-sw*s)/2;ty=(vh-sh*s)/2;apply();
}}
document.addEventListener('DOMContentLoaded',()=>{{
  const v=vp();if(!v)return;
  v.addEventListener('mousedown',e=>{{if(e.button!==0)return;drag=true;sx=e.clientX-tx;sy=e.clientY-ty;v.classList.add('grabbing');e.preventDefault();}});
  window.addEventListener('mousemove',e=>{{if(!drag)return;tx=e.clientX-sx;ty=e.clientY-sy;apply();}});
  window.addEventListener('mouseup',()=>{{drag=false;vp().classList.remove('grabbing');}});
  v.addEventListener('wheel',e=>{{e.preventDefault();const d=e.deltaY>0?-0.15:0.15;
    const r=v.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
    const os=s;s=Math.max(0.1,Math.min(20,s+d));const ratio=s/os;
    tx=mx-(mx-tx)*ratio;ty=my-(my-ty)*ratio;apply();}},{{passive:false}});
  const obs=new MutationObserver(()=>{{const svg=cv().querySelector('svg');if(svg){{obs.disconnect();
    const vb=svg.getAttribute('viewBox');if(vb){{const p=vb.split(/[\\s,]+/);svg.setAttribute('width',parseFloat(p[2]));svg.setAttribute('height',parseFloat(p[3]));}}
    svg.style.maxWidth='none';svg.style.overflow='visible';setTimeout(fit,50);}}}});
  obs.observe(cv(),{{childList:true,subtree:true}});
}});
</script>
</body></html>
"""


def _open_diagram_browser(unit):
    diagram = unit.get("planDiagram", "")
    if not diagram:
        return False
    html = _DIAGRAM_HTML.format(
        unit=unit.get("unit", "").replace("&", "&amp;").replace("<", "&lt;"),
        mermaid=diagram.replace("&", "&amp;").replace("<", "&lt;"),
    )
    fd, path = tempfile.mkstemp(suffix=".html", prefix="terrahawk_diagram_")
    with os.fdopen(fd, "w") as f:
        f.write(html)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


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

            full_path = unit.get("unit", "")
            segs = full_path.split("/")
            short_path = "/".join(segs[2:]) if len(segs) > 2 else (segs[-1] if segs else full_path)

            sc = {"clean": "+", "drift": "~", "error": "x", "timeout": "!"}.get(status, "?")
            has_diag = "D" if unit.get("planDiagram") else " "
            has_plan = "P" if unit.get("planResources") else " "
            ntags = len(unit.get("tags") or {})
            tag_ind = f"T{ntags}" if ntags else "  "

            right = f" {has_plan}{has_diag} {tag_ind:>3s} {res:>5s} {age:>6s}"
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


# ── Detail view ───────────────────────────────────────────────────

def _format_detail(unit):
    lines = []
    status = unit.get("status", "unknown")
    color = STATUS_COLORS.get(status, 7)

    lines.append((5, f" Unit: {unit['unit']}"))
    lines.append((color, f" Status: {status.upper()}"))
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
                lines.append((ac, f"   {r.get('action', '?'):8s}  {r.get('address', '?')}"))
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
            if dflt:
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


# ── Module info view ──────────────────────────────────────────────

def _table_row(cols, widths, pad=3):
    """Format a row of columns aligned to given widths."""
    parts = []
    for i, (col, w) in enumerate(zip(cols, widths)):
        col = str(col)
        if i == len(cols) - 1:
            parts.append(col)  # last column: no padding
        else:
            parts.append(f"{col:<{w}s}")
    return " " * pad + "  ".join(parts)


def _table_sep(widths, pad=3):
    """Horizontal separator matching column widths."""
    total = sum(widths) + 2 * (len(widths) - 1)
    return " " * pad + "\u2500" * total


def _format_module_info(unit):
    lines = []
    lines.append((5, f" Module Info \u2014 {unit.get('unit', '')}"))
    lines.append((7, ""))

    # Module source
    src = unit.get("moduleSource", "")
    if src:
        lines.append((5, " Source"))
        lines.append((7, f"   {src}"))
        lines.append((7, ""))

    # Summary line
    parts = []
    tf_ver = unit.get("tfVersion", "")
    if tf_ver:
        parts.append(f"Terraform: {tf_ver}")
    rc = unit.get("resourceCount", 0)
    parts.append(f"Resources: {rc}")
    age = unit.get("stateAgeDays")
    if age is not None:
        parts.append(f"State Age: {age}d")
    lines.append((7, " " + "  |  ".join(parts)))
    lines.append((7, ""))

    # ── Providers table ──────────────────────────────────────────
    providers = unit.get("providers", [])
    if providers:
        c_name = max(len("Name"), max(len(p.get("name", "")) for p in providers))
        c_ver = max(len("Version"), max(len(p.get("version", "-")) for p in providers))
        c_src = len("Source")
        widths = [c_name, c_ver, c_src]

        lines.append((5, f" Providers ({len(providers)})"))
        lines.append((5, _table_row(["Name", "Version", "Source"], widths)))
        lines.append((7, _table_sep(widths)))
        for p in providers:
            lines.append((7, _table_row(
                [p.get("name", "?"), p.get("version", "-"), p.get("source", "-")],
                widths,
            )))
        lines.append((7, ""))

    # ── Inputs table ─────────────────────────────────────────────
    inputs = unit.get("inputs", [])
    if inputs:
        c_name = max(len("Name"), max(len(i.get("name", "")) for i in inputs))
        c_type = max(len("Type"), max(len(i.get("type", "-")) or 1 for i in inputs))
        c_dflt = max(len("Default"), max(len(i.get("default", "-")) or 1 for i in inputs))
        # Cap columns so the table doesn't get absurdly wide
        c_type = min(c_type, 40)
        c_dflt = min(c_dflt, 40)
        widths = [c_name, c_type, c_dflt]

        lines.append((5, f" Input Variables ({len(inputs)})"))
        lines.append((5, _table_row(["Name", "Type", "Default"], widths)))
        lines.append((7, _table_sep(widths)))
        for inp in inputs:
            name = inp.get("name", "?")
            tp = inp.get("type", "-") or "-"
            dflt = inp.get("default", "-") or "-"
            # Truncate long values in the table
            tp_disp = tp[:c_type] if len(tp) > c_type else tp
            dflt_disp = dflt[:c_dflt] if len(dflt) > c_dflt else dflt
            lines.append((7, _table_row([name, tp_disp, dflt_disp], widths)))
            # Description on the next line if present
            desc = inp.get("description", "")
            if desc:
                lines.append((7, f"     \u2514 {desc}"))
        lines.append((7, ""))

    # ── Outputs table ────────────────────────────────────────────
    outputs = unit.get("outputs", {})
    if outputs:
        sorted_out = sorted(outputs.items())
        # Flatten values to single-line representations
        out_rows = []
        for k, v in sorted_out:
            val = json.dumps(v) if not isinstance(v, str) else v
            out_rows.append((k, val))

        c_name = max(len("Name"), max(len(k) for k, _ in out_rows))
        widths = [c_name]

        lines.append((5, f" Outputs ({len(outputs)})"))
        lines.append((5, _table_row(["Name", "Value"], [c_name, len("Value")])))
        lines.append((7, _table_sep([c_name, 40])))
        for k, val in out_rows:
            if len(val) <= 80:
                lines.append((7, _table_row([k, val], [c_name, len(val)])))
            else:
                # Long value: name on first line, value wrapped below
                lines.append((7, _table_row([k, val[:80] + "..."], [c_name, 83])))
                # Show the full value below for horizontal scrolling
                lines.append((7, f"     {val}"))
        lines.append((7, ""))

    # ── Tags table ───────────────────────────────────────────────
    tags = unit.get("tags")
    default_tags = unit.get("defaultTags", {})
    if tags:
        sorted_tags = sorted(tags.items())
        c_key = max(len("Key"), max(len(k) for k, _ in sorted_tags))
        c_val = max(len("Value"), max(len(v) for _, v in sorted_tags))
        c_val = min(c_val, 60)
        widths = [c_key, c_val, len("Source")]

        lines.append((5, f" Tags ({len(tags)})"))
        lines.append((5, _table_row(["Key", "Value", "Source"], widths)))
        lines.append((7, _table_sep(widths)))
        for k, v in sorted_tags:
            source = "default" if k in default_tags else "explicit"
            v_disp = v[:c_val] if len(v) > c_val else v
            lines.append((7, _table_row([k, v_disp, source], widths)))
        lines.append((7, ""))

    if not inputs and not outputs and not providers:
        lines.append((7, " No module information available. Run a full scan to populate."))

    return lines


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
        line = f"{prefix}{tag:12s} {r.get('type', ''):30s} {r.get('address', '?')}"
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


# ── Search prompt ─────────────────────────────────────────────────

def _input_prompt(stdscr, prompt_str, initial=""):
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    query = list(initial)
    while True:
        display = f" {prompt_str}" + "".join(query)
        _safe(stdscr, h - 1, 0, display + " " * max(0, w - len(display) - 1), w - 1, curses.color_pair(5))
        stdscr.move(h - 1, min(len(f" {prompt_str}") + len(query), w - 1))
        stdscr.refresh()
        k = stdscr.getch()
        if k in (curses.KEY_ENTER, 10, 13):
            break
        elif k == 27:
            query = list(initial)
            break
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            if query:
                query.pop()
        elif 32 <= k <= 126:
            query.append(chr(k))
    curses.curs_set(0)
    return "".join(query)


def _tag_prompt(stdscr, all_units, initial=""):
    """Tag filter prompt with Tab autocomplete and inline suggestions."""
    h, w = stdscr.getmaxyx()
    tag_keys, tag_pairs = _get_tag_completions(all_units)
    all_completions = tag_keys + tag_pairs  # keys first, then key=value pairs

    if not all_completions:
        # No tags in report — show message and return
        _safe(stdscr, h - 1, 0, " No tags found in this report." + " " * (w - 32),
              w - 1, curses.color_pair(3))
        stdscr.refresh()
        stdscr.getch()  # wait for any key to dismiss
        return None

    curses.curs_set(1)
    query = list(initial)
    suggestion = ""  # ghost text shown after cursor

    def _find_suggestion(text):
        """Find best completion matching the current input."""
        if not text:
            return ""
        t = text.lower()
        for c in all_completions:
            if c.lower().startswith(t) and c != text:
                return c[len(text):]
        return ""

    def _find_matches(text):
        """Find all completions matching the current input."""
        if not text:
            return all_completions[:10]
        t = text.lower()
        return [c for c in all_completions if t in c.lower()][:10]

    prompt_str = "tag filter: "

    while True:
        text = "".join(query)
        suggestion = _find_suggestion(text)
        matches = _find_matches(text)

        # Draw prompt line
        prefix = f" {prompt_str}"
        display = prefix + text
        _safe(stdscr, h - 1, 0, " " * (w - 1), w - 1, curses.color_pair(5))
        _safe(stdscr, h - 1, 0, display, w - 1, curses.color_pair(5))

        # Ghost suggestion after typed text
        if suggestion:
            ghost_x = len(display)
            if ghost_x < w - 1:
                _safe(stdscr, h - 1, ghost_x, suggestion[:w - ghost_x - 1],
                      w - ghost_x - 1, curses.A_DIM)

        # Show matches on line h-2 if there are any and input is non-empty
        if text and matches:
            match_line = " matches: " + "  ".join(matches)
            _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
            _safe(stdscr, h - 2, 0, match_line[:w - 1], w - 1, curses.A_DIM)
        else:
            _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)

        stdscr.move(h - 1, min(len(display), w - 1))
        stdscr.refresh()

        k = stdscr.getch()
        if k in (curses.KEY_ENTER, 10, 13):
            # Validate the input
            final = "".join(query)
            if final:
                # Check if it's a valid key or key=value
                if "=" in final:
                    tk, tv = final.split("=", 1)
                    valid = any(u.get("tags", {}).get(tk) == tv for u in all_units)
                else:
                    valid = final in tag_keys
                if not valid:
                    _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
                    msg = f" No units match tag '{final}'. Press any key."
                    _safe(stdscr, h - 2, 0, msg[:w - 1], w - 1, curses.color_pair(3))
                    stdscr.refresh()
                    stdscr.getch()
                    continue
            break
        elif k == 27:
            query = list(initial)
            break
        elif k == 9:  # Tab — accept suggestion
            if suggestion:
                query = list(text + suggestion)
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            if query:
                query.pop()
        elif 32 <= k <= 126:
            query.append(chr(k))

    # Clean up the match line
    _safe(stdscr, h - 2, 0, " " * (w - 1), w - 1, curses.A_DIM)
    curses.curs_set(0)
    return "".join(query) or None


# ── Main TUI ──────────────────────────────────────────────────────

def _tui_main(stdscr, all_units, report_path):
    _init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    # Enable mouse (scroll wheel + click)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    # Filters
    filter_status = None
    filter_sub = None
    filter_tag = None
    search_query = ""
    sort_mode = "status"

    # Derived
    subscriptions = _get_subscriptions(all_units)
    tag_keys = _get_tag_keys(all_units)

    filtered = _apply_filters(all_units, filter_status, filter_sub, filter_tag, search_query, sort_mode)
    rows = _build_rows(filtered)
    cursor = _snap_cursor(rows, 0, 1)
    scroll = 0

    mode = "list"
    detail_lines = []
    detail_scroll = 0
    detail_hscroll = 0
    detail_unit = None
    wrap = False
    # Plan state
    plan_rows = []
    plan_cursor = 0
    plan_scroll = 0
    plan_hscroll = 0
    plan_action_filter = None
    plan_type_filter = None
    plan_display_lines = []

    def rebuild():
        nonlocal filtered, rows, cursor, scroll
        filtered = _apply_filters(all_units, filter_status, filter_sub, filter_tag, search_query, sort_mode)
        rows = _build_rows(filtered)
        cursor = _snap_cursor(rows, 0, 1)
        scroll = 0

    def enter_detail(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        detail_unit = unit
        detail_lines = _format_detail(unit)
        detail_scroll = 0
        detail_hscroll = 0
        mode = "detail"

    def enter_module(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        detail_unit = unit
        detail_lines = _format_module_info(unit)
        detail_scroll = 0
        detail_hscroll = 0
        mode = "detail"

    def enter_diagram(unit):
        nonlocal detail_unit, detail_lines, detail_scroll, detail_hscroll, mode
        dl = _build_diagram_lines(unit)
        if dl:
            detail_unit = unit
            detail_lines = dl
            detail_scroll = 0
            detail_hscroll = 0
            mode = "detail"

    def enter_plan(unit):
        nonlocal detail_unit, plan_rows, plan_cursor, plan_scroll, plan_hscroll
        nonlocal plan_action_filter, plan_type_filter, plan_display_lines, mode
        if not unit.get("planResources"):
            return
        detail_unit = unit
        plan_action_filter = None
        plan_type_filter = None
        plan_rows = _build_plan_rows(unit.get("planResources", []), None, None)
        plan_cursor = 0
        plan_scroll = 0
        plan_hscroll = 0
        plan_display_lines = []
        mode = "plan"

    while True:
        try:
            h, w = stdscr.getmaxyx()
            if h < 5 or w < 20:
                stdscr.erase()
                _safe(stdscr, 0, 0, "Terminal too small", 18, curses.A_DIM)
                stdscr.refresh()
                stdscr.getch()
                continue
        except curses.error:
            continue

        if mode == "list":
            if not rows:
                stdscr.erase()
                _safe(stdscr, h // 2, max(0, (w - 30) // 2),
                      "No units match current filters.", w - 1, curses.color_pair(4))
                _safe(stdscr, h - 1, 0, " f status  s sub  t tag  / search  c clear  q quit",
                      w - 1, curses.A_DIM)
                stdscr.refresh()
            else:
                list_h = h - 4
                cursor = max(0, min(cursor, len(rows) - 1))
                if cursor < scroll:
                    scroll = cursor
                if cursor >= scroll + list_h:
                    scroll = cursor - list_h + 1
                scroll = max(0, scroll)
                _draw_list(stdscr, rows, cursor, scroll,
                           (filter_status, filter_sub, filter_tag, search_query),
                           filtered, sort_mode)

            key = stdscr.getch()

            # Resize
            if key == curses.KEY_RESIZE:
                continue

            # Mouse
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON1_CLICKED:
                        clicked_row = scroll + (my - 3)
                        if 0 <= clicked_row < len(rows) and rows[clicked_row][0] == "unit":
                            cursor = clicked_row
                    elif bstate & curses.BUTTON4_PRESSED:  # scroll up
                        if cursor > 0:
                            cursor = _snap_cursor(rows, cursor - 3, -1)
                    elif bstate & (curses.BUTTON5_PRESSED if hasattr(curses, 'BUTTON5_PRESSED') else 0):
                        if rows and cursor < len(rows) - 1:
                            cursor = _snap_cursor(rows, cursor + 3, 1)
                    # Also handle scroll down via REPORT_MOUSE_POSITION on some terminals
                    elif bstate & 0x00200000:  # scroll down fallback
                        if rows and cursor < len(rows) - 1:
                            cursor = _snap_cursor(rows, cursor + 3, 1)
                except curses.error:
                    pass
                continue

            if key == ord("q"):
                break
            elif key == curses.KEY_DOWN or key == ord("j"):
                if rows and cursor < len(rows) - 1:
                    cursor = _snap_cursor(rows, cursor + 1, 1)
            elif key == curses.KEY_UP or key == ord("k"):
                if cursor > 0:
                    cursor = _snap_cursor(rows, cursor - 1, -1)
            elif key == curses.KEY_NPAGE:
                if rows:
                    cursor = _snap_cursor(rows, min(cursor + (h - 4), len(rows) - 1), -1)
            elif key == curses.KEY_PPAGE:
                if rows:
                    cursor = _snap_cursor(rows, max(cursor - (h - 4), 0), 1)
            elif key == ord("g"):
                cursor = _snap_cursor(rows, 0, 1)
                scroll = 0
            elif key == ord("G"):
                if rows:
                    cursor = _snap_cursor(rows, len(rows) - 1, -1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if rows and rows[cursor][0] == "unit":
                    enter_detail(rows[cursor][1])
            elif key == ord("d"):
                if rows and rows[cursor][0] == "unit":
                    enter_diagram(rows[cursor][1])
            elif key == ord("D"):
                if rows and rows[cursor][0] == "unit":
                    _open_diagram_browser(rows[cursor][1])
            elif key == ord("p"):
                if rows and rows[cursor][0] == "unit":
                    enter_plan(rows[cursor][1])
            elif key == ord("m"):
                if rows and rows[cursor][0] == "unit":
                    enter_module(rows[cursor][1])
            elif key == ord("f"):
                cycle = [None, "drift", "error", "timeout", "clean"]
                idx = cycle.index(filter_status) if filter_status in cycle else 0
                filter_status = cycle[(idx + 1) % len(cycle)]
                rebuild()
            elif key == ord("s"):
                if subscriptions:
                    cycle = [None] + subscriptions
                    idx = cycle.index(filter_sub) if filter_sub in cycle else 0
                    filter_sub = cycle[(idx + 1) % len(cycle)]
                    rebuild()
            elif key == ord("t"):
                filter_tag = _tag_prompt(stdscr, all_units, filter_tag or "")
                rebuild()
            elif key == ord("o"):
                idx = SORT_MODES.index(sort_mode)
                sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
                rebuild()
            elif key == ord("/"):
                search_query = _input_prompt(stdscr, "/ ", search_query)
                rebuild()
            elif key == ord("c"):
                filter_status = None
                filter_sub = None
                filter_tag = None
                search_query = ""
                sort_mode = "status"
                rebuild()

        elif mode == "detail":
            detail_total = _draw_detail(stdscr, detail_lines, detail_scroll, detail_hscroll, wrap)
            max_scroll = max(0, detail_total - (h - 1))
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key in (27, ord("q"), ord("h"), curses.KEY_LEFT) and detail_hscroll == 0:
                mode = "list"
            elif key == curses.KEY_DOWN or key == ord("j"):
                if detail_scroll < max_scroll:
                    detail_scroll += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if detail_scroll > 0:
                    detail_scroll -= 1
            elif key == curses.KEY_NPAGE:
                detail_scroll = min(detail_scroll + (h - 2), max_scroll)
            elif key == curses.KEY_PPAGE:
                detail_scroll = max(detail_scroll - (h - 2), 0)
            elif key == curses.KEY_RIGHT or key == ord("l"):
                if not wrap:
                    detail_hscroll += 8
            elif key == curses.KEY_LEFT:
                if not wrap:
                    detail_hscroll = max(0, detail_hscroll - 8)
            elif key == ord("0"):
                detail_hscroll = 0
            elif key == ord("w"):
                wrap = not wrap
                detail_scroll = 0
                detail_hscroll = 0
            elif key == ord("g"):
                detail_scroll = 0
            elif key == ord("G"):
                detail_scroll = max_scroll
            elif key == ord("d"):
                if detail_unit:
                    enter_diagram(detail_unit)
            elif key == ord("D"):
                if detail_unit:
                    _open_diagram_browser(detail_unit)
            elif key == ord("p"):
                if detail_unit:
                    enter_plan(detail_unit)
            elif key == ord("m"):
                if detail_unit:
                    enter_module(detail_unit)

            # Mouse scroll in detail
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON4_PRESSED:
                        detail_scroll = max(0, detail_scroll - 3)
                    elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | 0x00200000):
                        detail_scroll = min(detail_scroll + 3, max(0, len(detail_lines) - (h - 1)))
                except curses.error:
                    pass

        elif mode == "plan":
            plan_display_lines = _draw_plan(
                stdscr, detail_unit, plan_rows, plan_cursor, plan_scroll,
                plan_action_filter, plan_type_filter, plan_hscroll, wrap,
            )
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key in (27, ord("q"), ord("h")) or (key == curses.KEY_LEFT and plan_hscroll == 0):
                mode = "list"
            elif key == curses.KEY_DOWN or key == ord("j"):
                if plan_display_lines and plan_cursor < len(plan_display_lines) - 1:
                    plan_cursor += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if plan_cursor > 0:
                    plan_cursor -= 1
            elif key == curses.KEY_NPAGE:
                if plan_display_lines:
                    plan_cursor = min(plan_cursor + (h - 4), len(plan_display_lines) - 1)
            elif key == curses.KEY_PPAGE:
                plan_cursor = max(plan_cursor - (h - 4), 0)
            elif key == curses.KEY_RIGHT or key == ord("l"):
                if not wrap:
                    plan_hscroll += 8
            elif key == curses.KEY_LEFT:
                if not wrap:
                    plan_hscroll = max(0, plan_hscroll - 8)
            elif key == ord("0"):
                plan_hscroll = 0
            elif key == ord("w"):
                wrap = not wrap
                plan_scroll = 0
                plan_hscroll = 0
            elif key == ord("g"):
                plan_cursor = 0
                plan_scroll = 0
            elif key == ord("G"):
                if plan_display_lines:
                    plan_cursor = len(plan_display_lines) - 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if plan_display_lines and plan_cursor < len(plan_display_lines):
                    ri, ltype, _, _ = plan_display_lines[plan_cursor]
                    if ltype == "header":
                        plan_rows[ri]["expanded"] = not plan_rows[ri]["expanded"]
                    elif ltype == "body":
                        plan_rows[ri]["expanded"] = False
            elif key == ord("f"):
                cycle = [None] + list(_ALL_ACTIONS)
                idx = cycle.index(plan_action_filter) if plan_action_filter in cycle else 0
                plan_action_filter = cycle[(idx + 1) % len(cycle)]
                plan_rows = _build_plan_rows(detail_unit.get("planResources", []), plan_action_filter, plan_type_filter)
                plan_cursor = 0
                plan_scroll = 0
            elif key == ord("t"):
                types = _get_plan_types(detail_unit.get("planResources", []))
                if types:
                    cycle = [None] + types
                    idx = cycle.index(plan_type_filter) if plan_type_filter in cycle else 0
                    plan_type_filter = cycle[(idx + 1) % len(cycle)]
                    plan_rows = _build_plan_rows(detail_unit.get("planResources", []), plan_action_filter, plan_type_filter)
                    plan_cursor = 0
                    plan_scroll = 0
            elif key == ord("d"):
                if detail_unit:
                    enter_diagram(detail_unit)
            elif key == ord("D"):
                if detail_unit:
                    _open_diagram_browser(detail_unit)

            # Mouse scroll in plan
            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON4_PRESSED:
                        plan_cursor = max(0, plan_cursor - 3)
                    elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | 0x00200000):
                        if plan_display_lines:
                            plan_cursor = min(plan_cursor + 3, len(plan_display_lines) - 1)
                except curses.error:
                    pass

            # Scroll management
            if plan_display_lines:
                plan_h = h - 4
                plan_cursor = max(0, min(plan_cursor, len(plan_display_lines) - 1))
                if plan_cursor < plan_scroll:
                    plan_scroll = plan_cursor
                if plan_cursor >= plan_scroll + plan_h:
                    plan_scroll = plan_cursor - plan_h + 1
                plan_scroll = max(0, plan_scroll)


def run_tui(report_name=None):
    report_path = _find_report(report_name)
    print(f"Loading {report_path.name}...")
    data = _load_report(report_path)
    if not data:
        print("\u274c Report is empty.")
        sys.exit(1)
    try:
        curses.wrapper(lambda stdscr: _tui_main(stdscr, data, report_path))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Ensure terminal is restored before printing
        print(f"\n\u274c TUI error: {e}", file=sys.stderr)
        sys.exit(1)
