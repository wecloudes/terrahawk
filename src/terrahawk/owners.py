"""Unit ownership from CODEOWNERS.

Maps each unit to the owners of its `terragrunt.hcl` using the repo's
CODEOWNERS file (GitHub / GitLab / Bitbucket locations), so every drift or
error in the report shows a responsible team.

Matching follows GitHub's CODEOWNERS rules (gitignore-style patterns, last
matching line wins):
- a leading `/` anchors to the repo root; a pattern with a `/` in the middle
  is also root-relative; a pattern without `/` matches at any depth
- `*` matches within one path segment, `**` across segments, `?` one char
- a pattern naming a directory covers everything below it, except `dir/*`,
  which (per GitHub) covers only the directory's direct children
- a matching line with no owners makes the path unowned

GitLab `[Section]` / `^[Section]` headers are understood; a section's
default owners apply to its entries that list none. Stdlib only.
"""

import os
import re

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS",
                        ".gitlab/CODEOWNERS", ".bitbucket/CODEOWNERS")

_SECTION_RE = re.compile(r"^\^?\[[^\]]+\](?:\[\d+\])?\s*(.*)$")


def find_codeowners(repo_root, override=None):
    """Path of the CODEOWNERS file to use, or None."""
    if override:
        p = os.path.join(str(repo_root), override)
        return p if os.path.isfile(p) else None
    for rel in CODEOWNERS_LOCATIONS:
        p = os.path.join(str(repo_root), rel)
        if os.path.isfile(p):
            return p
    return None


def _owner_tokens(tokens):
    return [t for t in tokens if t.startswith("@") or "@" in t]


def _pattern_re(pattern):
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    pat = pattern.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    i, out = 0, []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    prefix = "" if anchored else "(?:.*/)?"
    # A directory match covers its contents — except GitHub's `dir/*`, which
    # covers only direct children.
    suffix = "" if (pat.endswith("/*") and not dir_only) else "(?:/.*)?"
    if dir_only:
        suffix = "/.*"
    return re.compile(f"^{prefix}{body}{suffix}$")


def parse_codeowners(text):
    """Parse CODEOWNERS text into [(compiled_re, owners)] in file order."""
    rules = []
    section_default = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"\s+#.*$", "", line)
        m = _SECTION_RE.match(line)
        if m:
            section_default = _owner_tokens(m.group(1).split())
            continue
        tokens = line.split()
        pattern, owners = tokens[0], _owner_tokens(tokens[1:])
        if not owners and len(tokens) == 1:
            owners = list(section_default)
        try:
            rules.append((_pattern_re(pattern), owners))
        except re.error:
            continue
    return rules


def owners_for(rules, repo_rel_path):
    """Owners of a repo-relative file path (last matching rule wins)."""
    owners = []
    for rx, own in rules:
        if rx.match(repo_rel_path):
            owners = own
    return list(owners)


def unit_repo_path(repo_root, config_dir, rel_path):
    """Repo-relative path of a unit's terragrunt.hcl (posix separators)."""
    abs_hcl = os.path.join(str(config_dir), rel_path, "terragrunt.hcl")
    rel = os.path.relpath(abs_hcl, str(repo_root))
    return rel.replace(os.sep, "/")


def attach_owners(results, repo_root, config_dir, override=None):
    """Set `owners` on every entry. Returns the CODEOWNERS path or None."""
    path = find_codeowners(repo_root, override)
    if not path:
        return None
    try:
        rules = parse_codeowners(open(path, encoding="utf-8").read())
    except OSError:
        return None
    for e in results:
        e["owners"] = owners_for(rules, unit_repo_path(repo_root, config_dir, e["unit"]))
    return path
