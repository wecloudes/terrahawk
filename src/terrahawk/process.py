"""Result processing: transform raw worker results into final report entries."""

import json
import re
from datetime import datetime, timezone

from . import plan_parser


def _classify_status(exit_code, plan_output):
    """Classify unit status and extract diff/error from plan output."""
    status, diff, summary, error = "clean", "", "", ""

    if exit_code in (124, 137):
        status = "timeout"
        error = plan_output[-500:] if plan_output else "Plan timed out."
    elif exit_code == 0:
        status = "clean"
    elif exit_code == 2:
        status = "drift"
        m = re.search(r"(Terraform will perform|Terraform used the selected).*", plan_output, re.DOTALL)
        if m:
            diff = m.group(0)
            sep_idx = diff.rfind("\u2500" * 5)
            if sep_idx > 0:
                diff = diff[:sep_idx]
        else:
            diff = plan_output
        plan_lines = [l for l in plan_output.splitlines() if l.startswith("Plan:")]
        summary = plan_lines[-1] if plan_lines else ""
    else:
        status = "error"
        lines = plan_output.splitlines()
        error_lines, capture = [], False
        for line in lines:
            if re.match(r"^\s*(Error|╷|│\s*Error)", line, re.IGNORECASE):
                capture = True
            if capture:
                error_lines.append(line)
        error = "\n".join(error_lines[:30]) if error_lines else "\n".join(lines[-30:])

    return status, diff, summary, error



def _process_tags(raw, args):
    """Extract resource tags and default_tags."""
    tags = {}
    default_tags = {}
    if not args.tags:
        return tags, default_tags

    state = raw.get("state_json")
    if state:
        def extract_tags(mod):
            for res in mod.get("resources", []):
                vals = res.get("values", {})
                # Prefer tags_all (merged explicit + provider default_tags) when present
                rt = vals.get("tags_all") or vals.get("tags")
                if rt and isinstance(rt, dict):
                    tags.update(rt)
            for child in mod.get("child_modules", []):
                extract_tags(child)
        extract_tags(state.get("values", {}).get("root_module", {}))

    # Parse default_tags from the generated provider.tf (terragrunt generate block)
    pf_raw = raw.get("providers_tf", "")
    if pf_raw:
        dt_match = re.search(
            r'default_tags\s*\{\s*tags\s*=\s*\{([^}]*)\}\s*\}',
            pf_raw, re.DOTALL,
        )
        if dt_match:
            for km in re.finditer(
                r'([A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*"([^"]*)"',
                dt_match.group(1),
            ):
                default_tags[km.group(1)] = km.group(2)
            # Merge default_tags into tags if not already present from state
            for k, v in default_tags.items():
                tags.setdefault(k, v)

    return tags, default_tags


def _process_outputs(raw):
    """Extract outputs from terraform output JSON."""
    outputs = {}
    od = raw.get("outputs_json")
    if od:
        for k, v in od.items():
            outputs[k] = v.get("value", v) if isinstance(v, dict) else v
    return outputs


_SECRET_INPUT_RE = re.compile(
    r"password|passwd|secret|token|api_key|apikey|private_key|ssh_key|"
    r"credential|connection_string|sas_|client_secret|access_key",
    re.IGNORECASE,
)


def _rendered_input_value(name, value):
    """Format a rendered input value for display, masking likely secrets.

    The HTML report is published to static storage — never emit values whose
    input name looks credential-like.
    """
    if _SECRET_INPUT_RE.search(name):
        return "(masked)"
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= 200 else s[:200] + "…"


def _process_inputs(raw):
    """Parse input variables from variables.tf, enriched with rendered values."""
    inputs = _parse_variables_tf(raw)

    rendered = (raw.get("render_json") or {}).get("inputs") or {}
    if rendered:
        declared = {i["name"] for i in inputs}
        for i in inputs:
            if i["name"] in rendered:
                i["value"] = _rendered_input_value(i["name"], rendered[i["name"]])
        # Inputs passed by terragrunt but not declared in variables.tf
        for name in sorted(set(rendered) - declared):
            inputs.append({"name": name,
                           "value": _rendered_input_value(name, rendered[name])})
    return inputs


def _parse_variables_tf(raw):
    """Parse input variable declarations from variables.tf."""
    inputs = []
    vf = raw.get("variables_tf", "")
    if not vf:
        return inputs
    for m in re.finditer(r'variable\s+"(\w+)"\s*\{(.*?)\n\}', vf, re.DOTALL):
        name, body = m.group(1), m.group(2)
        v = {"name": name}
        tm = re.search(r"type\s*=\s*(.+)", body)
        if tm:
            tval = tm.group(1).strip()
            depth = tval.count("(") - tval.count(")") + tval.count("{") - tval.count("}")
            if depth > 0:
                for ln in body[body.index(tval) + len(tval):].split("\n"):
                    tval += "\n" + ln
                    depth += ln.count("(") - ln.count(")") + ln.count("{") - ln.count("}")
                    if depth <= 0:
                        break
            v["type"] = tval.strip()
        dm = re.search(r"default\s*=\s*(.+?)$", body, re.MULTILINE)
        if dm:
            v["default"] = dm.group(1).strip()
        desc = re.search(r'description\s*=\s*"(.*?)"', body)
        if not desc:
            desc = re.search(r"description\s*=\s*<<-?(\w+)\s*\n(.*?)\n\s*\1", body, re.DOTALL)
        if desc:
            v["description"] = desc.group(len(desc.groups())).strip()[:200]
        inputs.append(v)
    return inputs


def _process_providers(raw, root_provider_tpl):
    """Extract provider requirements and terraform version."""
    providers, tf_version = [], ""
    pf = raw.get("providers_tf", "")
    if root_provider_tpl and root_provider_tpl not in pf:
        pf = (pf + "\n\n" + root_provider_tpl) if pf else root_provider_tpl
    if not pf:
        return providers, tf_version

    tvm = re.search(r'required_version\s*=\s*"([^"]+)"', pf)
    if tvm:
        tf_version = tvm.group(1)
    seen_providers = set()
    # Find each `required_providers {` and use a brace counter to grab
    # the full block (regex alone can't handle nested braces).
    i = 0
    while True:
        m = re.search(r'required_providers\s*\{', pf[i:])
        if not m:
            break
        start = i + m.end()
        depth, j = 1, start
        while j < len(pf) and depth > 0:
            if pf[j] == "{":
                depth += 1
            elif pf[j] == "}":
                depth -= 1
            j += 1
        block = pf[start:j - 1]
        i = j
        # Inside the block, each provider is `name = { source = "..." version = "..." }`
        for pm in re.finditer(r'(\w+)\s*=\s*\{([^{}]*)\}', block):
            name = pm.group(1)
            if name in seen_providers:
                continue
            seen_providers.add(name)
            src = re.search(r'source\s*=\s*"([^"]+)"', pm.group(2))
            ver = re.search(r'version\s*=\s*"([^"]+)"', pm.group(2))
            providers.append({
                "name": name,
                "source": src.group(1) if src else "",
                "version": ver.group(1) if ver else "",
            })
    # Also pick up legacy `provider "X" { version = "..." }` blocks (the
    # shape Terragrunt's root generate block typically emits). These are
    # deprecated in Terraform but still valid, and without handling them
    # the provider list ends up empty.
    for pm in re.finditer(r'provider\s+"([^"]+)"\s*\{', pf):
        name = pm.group(1)
        if name in seen_providers:
            continue
        # Walk braces to find the block body
        start = pm.end()
        depth, j = 1, start
        while j < len(pf) and depth > 0:
            if pf[j] == "{":
                depth += 1
            elif pf[j] == "}":
                depth -= 1
            j += 1
        body = pf[start:j - 1]
        ver = re.search(r'version\s*=\s*"([^"]+)"', body)
        if not ver:
            continue
        seen_providers.add(name)
        providers.append({
            "name": name,
            # No explicit source in legacy syntax — Terraform defaults
            # to the hashicorp/ namespace.
            "source": f"hashicorp/{name}",
            "version": ver.group(1),
        })

    return providers, tf_version


def _generate_plan_diagram(raw, plan_resources):
    """Build a Mermaid flowchart showing the full unit state with plan changes highlighted.

    Merges resources from the current Terraform state (via `state_json`) with
    planned changes (via `plan_resources`).  Unchanged resources get a neutral
    style; changed resources are color-coded by action.  Dependencies come from
    the `configuration` section of `terraform show -json <planfile>`.
    """
    if not plan_resources:
        return ""

    plan_json = raw.get("plan_json")
    state_json = raw.get("state_json")

    # ── 1. Collect ALL resource addresses ──────────────────────
    # From plan changes
    changed_addrs = {r["address"] for r in plan_resources}
    action_map = {r["address"]: r["action"] for r in plan_resources}
    type_map = {r["address"]: r["type"] for r in plan_resources}

    # From current state (all existing resources)
    def _extract_state_resources(mod, prefix=""):
        """Recursively extract resource addresses and types from state JSON."""
        for res in mod.get("resources", []):
            addr = res.get("address", "")
            if prefix:
                addr = f"{prefix}.{addr}"
            rtype = res.get("type", "")
            if addr not in type_map:
                type_map[addr] = rtype
        for child in mod.get("child_modules", []):
            child_addr = child.get("address", "")
            _extract_state_resources(child, "")  # child already has full address

    if state_json:
        root_mod = state_json.get("values", {}).get("root_module", {})
        _extract_state_resources(root_mod)

    all_addrs = set(type_map.keys())

    # ── 2. Extract dependency references from plan configuration ──
    all_refs = {}  # address -> set of referenced addresses

    def _collect_references(obj, refs):
        """Recursively walk an expression tree collecting all 'references' lists."""
        if isinstance(obj, dict):
            for ref in obj.get("references", []):
                refs.add(ref)
            for v in obj.values():
                _collect_references(v, refs)
        elif isinstance(obj, list):
            for item in obj:
                _collect_references(item, refs)

    def _extract_refs(mod, prefix=""):
        for res in mod.get("resources", []):
            addr = res.get("address", "")
            if prefix:
                addr = f"{prefix}.{addr}"
            refs = set()
            # Expression references (attribute values that reference other resources)
            for attr, expr in res.get("expressions", {}).items():
                _collect_references(expr, refs)
            # Explicit depends_on
            for dep in res.get("depends_on", []):
                refs.add(dep)
            all_refs[addr] = refs
        for child in mod.get("module_calls", {}).values():
            child_mod = child.get("module", {})
            child_prefix = f"module.{child.get('name', '')}" if not prefix else f"{prefix}.module.{child.get('name', '')}"
            _extract_refs(child_mod, child_prefix)

    if plan_json:
        config = plan_json.get("configuration", {}).get("root_module", {})
        _extract_refs(config)

    # ── 3. Build edges between ALL known resources ─────────────
    def _strip_each_key(addr):
        """Strip for_each/count keys: aws_foo.bar["key"] → aws_foo.bar"""
        return re.sub(r'\[.*?\]', '', addr)

    # Skip references to non-resource objects
    _SKIP_PREFIXES = ("var.", "local.", "each.", "self.", "count.", "path.", "terraform.", "null_resource.")

    def _normalize_ref(ref):
        """Normalize a reference string to a base resource address.

        Handles regular resources (type.name), data sources (data.type.name),
        and module resources (module.name.type.name).
        """
        if any(ref.startswith(p) for p in _SKIP_PREFIXES):
            return None
        parts = ref.split(".")
        # module.X.type.name or module.X.data.type.name
        if parts[0] == "module" and len(parts) >= 4:
            if parts[2] == "data" and len(parts) >= 5:
                return _strip_each_key(".".join(parts[:5]))
            return _strip_each_key(".".join(parts[:4]))
        # data.type.name
        if parts[0] == "data" and len(parts) >= 3:
            return _strip_each_key(f"data.{parts[1]}.{parts[2]}")
        # type.name
        if len(parts) >= 2:
            return _strip_each_key(f"{parts[0]}.{parts[1]}")
        return None

    # Build a lookup from base address → list of actual state addresses
    # so config refs (without for_each keys) can find state resources (with keys)
    base_to_actual = {}  # "aws_foo.bar" → {"aws_foo.bar[0]", "aws_foo.bar[\"x\"]", ...}
    for addr in all_addrs:
        base = _strip_each_key(addr)
        base_to_actual.setdefault(base, set()).add(addr)

    # Also map config addresses (without for_each) to actual addresses
    config_base_to_actual = {}
    for cfg_addr in all_refs:
        base = _strip_each_key(cfg_addr)
        config_base_to_actual.setdefault(base, set()).add(cfg_addr)

    edges = []
    for cfg_addr, refs in all_refs.items():
        cfg_base = _strip_each_key(cfg_addr)
        # Find actual state addresses that match this config address
        src_addrs = base_to_actual.get(cfg_base, set())
        if not src_addrs:
            continue
        for ref in refs:
            ref_base = _normalize_ref(ref)
            if not ref_base:
                continue
            # Find actual state addresses that match the referenced resource
            dst_addrs = base_to_actual.get(ref_base, set())
            for src in src_addrs:
                for dst in dst_addrs:
                    if src != dst:
                        edges.append((dst, src))  # dependency → dependent

    # ── 4. Build Mermaid ───────────────────────────────────────
    action_style = {
        "create": "addCls", "update": "updCls", "delete": "delCls",
        "replace": "repCls", "read": "readCls",
    }
    action_icon = {"create": "+", "update": "~", "delete": "-", "replace": "*", "read": "?"}

    def _mermaid_escape(s):
        return s.replace('"', '#34;').replace('[', '#91;').replace(']', '#93;')

    def _short_label(addr):
        parts = addr.split(".")
        # data.type.name → data.name
        if parts[0] == "data" and len(parts) >= 3:
            return f"data.{'.'.join(parts[2:])}"
        # module.X.data.type.name → X.data.name
        if parts[0] == "module" and len(parts) >= 5 and parts[2] == "data":
            return f"{parts[1]}.data.{'.'.join(parts[4:])}"
        # module.X.type.name → X.name
        if parts[0] == "module" and len(parts) >= 4:
            return f"{parts[1]}.{'.'.join(parts[3:])}"
        elif len(parts) >= 2:
            return ".".join(parts[1:])
        return addr

    def _is_data_source(addr):
        parts = addr.split(".")
        return parts[0] == "data" or (parts[0] == "module" and len(parts) >= 3 and parts[2] == "data")

    lines = ["flowchart LR"]
    lines.append("  classDef addCls fill:#12261e,stroke:#3fb950,color:#3fb950")
    lines.append("  classDef updCls fill:#2d1a08,stroke:#e8863a,color:#e8863a")
    lines.append("  classDef delCls fill:#2d1214,stroke:#f85149,color:#f85149")
    lines.append("  classDef repCls fill:#2d1a08,stroke:#d29922,color:#d29922")
    lines.append("  classDef readCls fill:#0d2138,stroke:#58a6ff,color:#58a6ff")
    lines.append("  classDef stableCls fill:#161b22,stroke:#30363d,color:#8b949e")
    lines.append("  classDef dataCls fill:#0d2138,stroke:#1a3a5c,color:#58a6ff,stroke-dasharray:5 5")

    # Assign node IDs
    node_ids = {}
    for i, addr in enumerate(sorted(all_addrs)):
        node_ids[addr] = f"n{i}"

    # Add nodes
    for addr in sorted(all_addrs):
        nid = node_ids[addr]
        rtype = type_map.get(addr, "")
        label = _mermaid_escape(_short_label(addr))
        stype = _mermaid_escape(rtype)

        if addr in changed_addrs:
            action = action_map[addr]
            icon = action_icon.get(action, "?")
            cls = action_style.get(action, "updCls")
            lines.append(f'  {nid}["{icon} <b>{label}</b><br><sub>{stype}</sub>"]:::{cls}')
        elif _is_data_source(addr):
            lines.append(f'  {nid}["{label}<br><sub>{stype}</sub>"]:::dataCls')
        else:
            lines.append(f'  {nid}["{label}<br><sub>{stype}</sub>"]:::stableCls')

    # Add edges
    seen_edges = set()
    for src, dst in edges:
        key = (src, dst)
        if key not in seen_edges:
            seen_edges.add(key)
            lines.append(f"  {node_ids[src]} --> {node_ids[dst]}")

    return "\n".join(lines)


def _process_module_source(raw):
    """Extract module source: rendered config first, terragrunt.hcl regex fallback."""
    src = ((raw.get("render_json") or {}).get("terraform") or {}).get("source")
    if src:
        return src
    tg = raw.get("tg_hcl", "")
    if tg:
        sm = re.search(r'source\s*=\s*"([^"]+)"', tg)
        if sm:
            return sm.group(1)
    return ""


def _compute_state_age(rel_path, blob_dates, raw=None):
    """Compute state age in days from blob dates.

    Prefers the exact state key from the rendered remote_state config
    (no path heuristics); falls back to rel_path + '/terraform.tfstate'.
    """
    lm = None
    cfg = ((raw or {}).get("render_json") or {}).get("remote_state") or {}
    exact_key = (cfg.get("config") or {}).get("key") or (cfg.get("config") or {}).get("prefix")
    if exact_key:
        lm = blob_dates.get(exact_key)
    if not lm:
        blob_key = rel_path + "/terraform.tfstate"
        lm = blob_dates.get(blob_key)
    if not lm:
        return None
    try:
        lm_dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - lm_dt).days
    except Exception:
        return None


def process_result(raw, args, blob_dates, root_provider_tpl=""):
    """Process raw worker result into final report entry."""
    rel_path = raw["unit"]
    exit_code = raw.get("exit_code", 1)
    plan_output = raw.get("plan_output", "")

    segments = rel_path.split("/")
    env = segments[0] if len(segments) > 0 else ""
    sub = segments[1] if len(segments) > 1 else ""
    reg = segments[2] if len(segments) > 2 else ""
    app = segments[3] if len(segments) > 3 else ""

    # Strip terragrunt STDOUT prefix
    plan_output = re.sub(r"^\d{2}:\d{2}:\d{2}\.\d+ STDOUT terraform: ?", "", plan_output, flags=re.MULTILINE)

    status, diff, summary, error = _classify_status(exit_code, plan_output)

    # Parse plan resources (for drift units, populate structured change list).
    # Text parse first (best diff bodies); JSON plan as fallback when the
    # text parser comes up empty.
    plan_resources = []
    if status == "drift":
        plan_resources = plan_parser.parse_plan_resources(plan_output)
        if not plan_resources and raw.get("plan_json"):
            plan_resources = plan_parser.parse_plan_resources_json(raw["plan_json"])

    # Out-of-band drift: resources changed outside Terraform. Present even on
    # clean plans (exit 0), where the text output never mentions them.
    drifted_resources = plan_parser.extract_resource_drift(raw.get("plan_json"))

    plan_diagram = _generate_plan_diagram(raw, plan_resources)
    tags, default_tags = _process_tags(raw, args)
    outputs = _process_outputs(raw)
    inputs = _process_inputs(raw)
    providers, tf_version = _process_providers(raw, root_provider_tpl)
    module_source = _process_module_source(raw)
    resource_count = raw.get("resource_count", 0)
    state_age_days = _compute_state_age(rel_path, blob_dates, raw)

    return {
        "unit": rel_path, "status": status, "exit_code": exit_code,
        "environment": env, "subscription": sub, "region": reg, "application": app,
        "summary": summary, "diff": diff, "error": error,
        "planResources": plan_resources,
        "planDiagram": plan_diagram,
        "tags": tags if args.tags else None,
        "defaultTags": default_tags if args.tags else {},
        "inputs": inputs, "outputs": outputs,
        "providers": providers, "tfVersion": tf_version,
        "moduleSource": module_source, "resourceCount": resource_count,
        "stateAgeDays": state_age_days,
        "duration": raw.get("duration"),
        "driftedResources": drifted_resources,
    }
