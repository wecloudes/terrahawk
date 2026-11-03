"""Known-drift ignore rules (`.terrahawkignore`).

A drift scan that runs daily keeps reporting the same accepted drift (tags
managed by an external tagging service, autoscaling counts, ...). Ignore rules
suppress it so the report only shows drift someone needs to act on.

File format — one rule per line, whitespace-separated fields:

    # <unit> <address> [<attr>[,<attr>...]] [until=YYYY-MM-DD] [# reason]
    *            *                     tags,tags_all      # tagging service owns tags
    prod/**      aws_autoscaling_group.*  desired_capacity
    shared/vpc   aws_security_group.legacy  until=2026-12-31  # removal ticket open

- `unit` matches the unit's rel_path (or its stack display path); `address`
  matches the resource address. `*` matches any run of characters (including
  `/` and `.`), `?` one character; everything else is literal, so `for_each`
  addresses like `aws_instance.web["a"]` need no escaping.
- Without attributes the rule ignores the whole resource change. With
  attributes it only ignores an update/replace whose *every* changed attribute
  path is covered; an attribute pattern also covers nested paths (`tags`
  covers `tags.Owner`).
- `until=` makes the rule expire (inclusive), so accepted drift resurfaces
  instead of being hidden forever.

A drift unit whose every planned change is ignored is reported clean, with
the original status kept in `ignoredStatus`. Ignored resources stay in the
report, flagged `ignored` (the rule's reason or text). Stdlib only.
"""

import re
from datetime import date

DEFAULT_IGNORE_FILE = ".terrahawkignore"


def _glob_re(pattern, attr=False):
    """Compile a `*`/`?` glob (everything else literal). Attribute patterns
    also match nested paths below the pattern."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    body = "".join(out)
    if attr:
        body += r"(?:\..*)?"
    return re.compile(rf"^{body}$")


class Rule:
    __slots__ = ("line", "text", "unit", "address", "attrs", "until", "reason",
                 "_unit_re", "_addr_re", "_attr_res", "hits")

    def __init__(self, line, text, unit, address, attrs, until, reason):
        self.line, self.text = line, text
        self.unit, self.address, self.attrs = unit, address, attrs
        self.until, self.reason = until, reason
        self._unit_re = _glob_re(unit)
        self._addr_re = _glob_re(address)
        self._attr_res = [_glob_re(a, attr=True) for a in attrs]
        self.hits = 0

    def label(self):
        return self.reason or self.text

    def expired(self, today):
        return self.until is not None and today > self.until

    def matches_unit(self, entry):
        return any(self._unit_re.match(u) for u in
                   (entry.get("unit", ""), entry.get("displayUnit", "")) if u)

    def matches_resource(self, address, action, changed_attrs):
        if not self._addr_re.match(address or ""):
            return False
        if not self.attrs:
            return True
        if action not in ("update", "replace", None) or not changed_attrs:
            return False
        return all(any(r.match(p) for r in self._attr_res) for p in changed_attrs)


def parse_rules(text):
    """Parse ignore-file text. Returns (rules, warnings)."""
    rules, warnings = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        reason = ""
        m = re.search(r"\s#\s?(.*)$", line)
        if m:
            reason = m.group(1).strip()
            line = line[:m.start()].strip()
        tokens = line.split()
        until = None
        fields = []
        bad = False
        for tok in tokens:
            if tok.startswith("until="):
                try:
                    until = date.fromisoformat(tok[len("until="):])
                except ValueError:
                    warnings.append(f"line {n}: bad date in '{tok}' (want until=YYYY-MM-DD)")
                    bad = True
            else:
                fields.append(tok)
        if bad:
            continue
        if len(fields) < 2 or len(fields) > 3:
            warnings.append(f"line {n}: expected '<unit> <address> [<attrs>]', got '{raw.strip()}'")
            continue
        attrs = [a for a in fields[2].split(",") if a] if len(fields) == 3 else []
        rules.append(Rule(n, raw.strip(), fields[0], fields[1], attrs, until, reason))
    return rules, warnings


def load_rules(repo_root, path=None):
    """Load rules from `path` (relative to repo_root) or `.terrahawkignore`.

    Returns (rules, warnings, file_path_or_None). A missing default file is
    not an error; a missing explicitly configured file is a warning.
    """
    explicit = bool(path)
    p = (repo_root / path) if path else (repo_root / DEFAULT_IGNORE_FILE)
    if not p.is_file():
        return [], ([f"ignore file not found: {p}"] if explicit else []), None
    rules, warnings = parse_rules(p.read_text())
    return rules, warnings, p


def _first_match(rules, entry, address, action, attrs):
    for rule in rules:
        if rule.matches_unit(entry) and rule.matches_resource(address, action, attrs):
            return rule
    return None


def apply_rules(results, rules, today=None):
    """Flag ignored changes on report entries, in place. Idempotent.

    Entries whose status was suppressed by an earlier application (e.g. a
    merged incremental entry) are restored first, so the *current* rules
    decide. Returns the list of expired rules (not applied).
    """
    today = today or date.today()
    active = [r for r in rules if not r.expired(today)]
    expired = [r for r in rules if r.expired(today)]

    for e in results:
        # Restore a previous application.
        if e.get("ignoredStatus"):
            e["status"] = e.pop("ignoredStatus")
        e.pop("ignoredCount", None)
        for r in (e.get("planResources") or []):
            r.pop("ignored", None)
        for r in (e.get("driftedResources") or []):
            r.pop("ignored", None)
        if not active:
            continue

        count = 0
        plan_res = e.get("planResources") or []
        for r in plan_res:
            rule = _first_match(active, e, r.get("address"), r.get("action"),
                                r.get("changedAttrs"))
            if rule:
                r["ignored"] = rule.label()
                rule.hits += 1
                count += 1
        for r in (e.get("driftedResources") or []):
            rule = _first_match(active, e, r.get("address"), None, r.get("changedAttrs"))
            if rule:
                r["ignored"] = rule.label()
                rule.hits += 1
                count += 1
        if count:
            e["ignoredCount"] = count
        if (e.get("status") == "drift" and plan_res
                and all(r.get("ignored") for r in plan_res)):
            e["ignoredStatus"] = "drift"
            e["status"] = "clean"
    return expired
