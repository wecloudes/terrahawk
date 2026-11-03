"""Module info view: providers, inputs, outputs and tags tables."""

import json


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
        has_vals = any("value" in i for i in inputs)
        c_name = max(len("Name"), max(len(i.get("name", "")) for i in inputs))
        c_type = max(len("Type"), max(len(i.get("type", "-")) or 1 for i in inputs))
        c_dflt = max(len("Default"), max(len(i.get("default", "-")) or 1 for i in inputs))
        # Cap columns so the table doesn't get absurdly wide
        c_type = min(c_type, 40)
        c_dflt = min(c_dflt, 40)
        headers = ["Name", "Type", "Default"]
        widths = [c_name, c_type, c_dflt]
        if has_vals:
            c_val = min(max(len("Value"), max(len(str(i.get("value", "-"))) for i in inputs)), 40)
            headers.insert(2, "Value")
            widths.insert(2, c_val)

        lines.append((5, f" Input Variables ({len(inputs)})"))
        lines.append((5, _table_row(headers, widths)))
        lines.append((7, _table_sep(widths)))
        for inp in inputs:
            name = inp.get("name", "?")
            tp = inp.get("type", "-") or "-"
            dflt = inp.get("default", "-") or "-"
            # Truncate long values in the table
            tp_disp = tp[:c_type] if len(tp) > c_type else tp
            dflt_disp = dflt[:c_dflt] if len(dflt) > c_dflt else dflt
            cells = [name, tp_disp, dflt_disp]
            if has_vals:
                val = str(inp.get("value", "-"))
                cells.insert(2, val[:c_val] if len(val) > c_val else val)
            lines.append((7, _table_row(cells, widths)))
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
