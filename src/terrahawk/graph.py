"""Repo-level unit dependency graph: terragrunt dag graph -> Mermaid."""

import os
import re
import subprocess

from .deps import mise_cmd

_DOT_EDGE_RE = re.compile(r'"([^"]+)"\s*->\s*"([^"]+)"')
_DOT_NODE_RE = re.compile(r'^\s*"([^"]+)"\s*;', re.MULTILINE)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Terrahawk — Unit Dependency Graph</title>
<style>
body{margin:0;padding:24px;background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif}
h1{font-size:18px;font-weight:600}
.mermaid{background:#0d1117}
</style>
</head>
<body>
<h1>🦅 Terrahawk — Unit Dependency Graph</h1>
<pre class="mermaid">
%%MERMAID%%
</pre>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({startOnLoad:true,theme:"dark",flowchart:{rankSpacing:60}});
</script>
</body>
</html>
"""


def get_dag_dot(config_dir, tg_ver=""):
    """Run `terragrunt dag graph` and return DOT output, or None on failure."""
    cfg = os.path.realpath(str(config_dir))
    cmd = mise_cmd("terragrunt", tg_ver, ["dag", "graph", f"--working-dir={cfg}"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or "digraph" not in r.stdout:
            return None
        return r.stdout
    except Exception:
        return None


def dot_to_mermaid(dot):
    """Convert terragrunt's DOT digraph into a Mermaid flowchart."""
    edges = _DOT_EDGE_RE.findall(dot)
    nodes = set(_DOT_NODE_RE.findall(dot))
    for a, b in edges:
        nodes.update((a, b))

    ids = {}

    def nid(name):
        if name not in ids:
            ids[name] = "n" + re.sub(r"[^A-Za-z0-9_]", "_", name)
        return ids[name]

    lines = ["graph TD"]
    targets = {b for _, b in edges}
    sources = {a for a, _ in edges}
    for name in sorted(nodes):
        # Roots of the DAG (no dependencies) get a distinct shape
        shape = f'{nid(name)}[["{name}"]]' if name in targets and name not in sources \
            else f'{nid(name)}["{name}"]'
        lines.append(f"    {shape}")
    for a, b in sorted(edges):
        lines.append(f"    {nid(a)} --> {nid(b)}")
    return "\n".join(lines)


def cmd_graph(config_dir, tg_ver="", output=None):
    """`terrahawk graph` entry: print Mermaid or write a self-contained HTML.

    Returns process exit code.
    """
    dot = get_dag_dot(config_dir, tg_ver)
    if dot is None:
        print("❌ `terragrunt dag graph` failed — requires Terragrunt 1.x.")
        return 1
    mermaid = dot_to_mermaid(dot)
    if output:
        html = _HTML_TEMPLATE.replace("%%MERMAID%%", mermaid)
        with open(output, "w") as f:
            f.write(html)
        print(f"📊 Dependency graph written to {output}")
    else:
        print(mermaid)
    return 0
