"""Architecture diagram rendering: in-terminal text view and browser export."""

import os
import re as _re
import subprocess
import sys
import tempfile


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
