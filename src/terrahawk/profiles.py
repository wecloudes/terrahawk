"""Multi-account AWS profile resolution.

Lets a single scan span several AWS accounts by mapping each unit to the
credential profile that owns its account. Terragrunt's generated provider pins
`allowed_account_ids` per unit, so the wrong profile fails the plan — this maps
unit -> profile up front so every unit plans with the right credentials and all
accounts land in ONE report.

Mapping (for 2+ profiles): resolve each profile's account via
`aws sts get-caller-identity`, resolve each unit's account from its nearest
`env.hcl` (`aws_account_id`), and match. A single profile short-circuits — every
unit uses it, no STS calls. Leaf module: stdlib + `aws` CLI only, no imports
from other terrahawk modules (keeps the import DAG acyclic).
"""

import os
import re
import subprocess

_ACCT_LINE_RE = re.compile(r'aws_account_id\s*=\s*(.+)')
_GET_ENV_RE = re.compile(r'get_env\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')
_LITERAL_RE = re.compile(r'"(\d{6,})"')
_LOCAL_REF_RE = re.compile(r'local\.(\w+)')
_STATE_ACCT_RE = re.compile(r'state_account_id\s*=\s*"(\d+)"')


def _find_env_hcl(unit_dir, config_dir):
    """Nearest `env.hcl` at or above unit_dir, not above config_dir."""
    d = os.path.realpath(unit_dir)
    stop = os.path.realpath(config_dir)
    while True:
        p = os.path.join(d, "env.hcl")
        if os.path.isfile(p):
            return p
        if d == stop:
            return None
        parent = os.path.dirname(d)
        if parent == d:  # filesystem root
            return None
        d = parent


def _resolve_local(name, hcl_path):
    """Resolve `local.<name>` from the file's own or sibling `.hcl` locals."""
    d = os.path.dirname(hcl_path)
    candidates = [hcl_path]
    try:
        candidates += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".hcl")]
    except OSError:
        pass
    pat = re.compile(re.escape(name) + r'\s*=\s*"([^"]+)"')
    for c in candidates:
        try:
            m = pat.search(open(c).read())
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def _extract_account(env_hcl_path):
    """Extract the 12-digit AWS account id declared in an `env.hcl`.

    Handles `get_env("VAR", "default")` (env var wins over the default),
    bare string literals, and `local.X` / `${local.X}` interpolations.
    """
    try:
        txt = open(env_hcl_path).read()
    except OSError:
        return None
    m = _ACCT_LINE_RE.search(txt)
    if not m:
        return None
    expr = m.group(1).strip()
    g = _GET_ENV_RE.search(expr)
    if g:
        return os.environ.get(g.group(1)) or g.group(2)
    lit = _LITERAL_RE.search(expr)
    if lit:
        return lit.group(1)
    loc = _LOCAL_REF_RE.search(expr)
    if loc:
        return _resolve_local(loc.group(1), env_hcl_path)
    return None


def resolve_unit_accounts(units, config_dir):
    """{rel_path: account_id} from each unit's nearest env.hcl (None if unknown)."""
    out = {}
    for unit_dir, rel_path in units:
        env = _find_env_hcl(unit_dir, config_dir)
        out[rel_path] = _extract_account(env) if env else None
    return out


def profile_accounts(profile_names):
    """{profile: account_id} via `aws sts get-caller-identity`. Warns and skips
    any profile that can't be resolved (not logged in, bad name)."""
    out = {}
    for name in profile_names:
        try:
            r = subprocess.run(
                ["aws", "sts", "get-caller-identity",
                 "--profile", name, "--query", "Account", "--output", "text"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:  # noqa: BLE001 — surface, never crash the scan
            print(f"  ⚠ profile '{name}': sts get-caller-identity failed ({e})")
            continue
        acct = (r.stdout or "").strip()
        if r.returncode == 0 and acct.isdigit():
            out[name] = acct
        else:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"  ⚠ profile '{name}': could not resolve account "
                  f"({detail[-1] if detail else 'unknown error'})")
    return out


def _account_to_profile(profile_names, prof_acct):
    """{account_id: profile}, first profile winning a shared account."""
    acct_prof = {}
    for name in profile_names:
        acct = prof_acct.get(name)
        if acct and acct not in acct_prof:
            acct_prof[acct] = name
    return acct_prof


def build_unit_profile_map(units, config_dir, profile_names):
    """Map units to AWS profiles for a multi-account scan.

    Returns (unit_profiles, account_to_profile):
      - unit_profiles: {rel_path: profile} for units whose account matched a
        profile (units with no match are omitted → default credential chain).
      - account_to_profile: {account_id: profile} (empty for the single-profile
        short-circuit) — lets the caller pick a profile for global operations
        such as the state-bucket listing.

    A single profile short-circuits: every unit maps to it, no STS calls.
    """
    if not profile_names:
        return {}, {}
    if len(profile_names) == 1:
        return {rp: profile_names[0] for _, rp in units}, {}

    prof_acct = profile_accounts(profile_names)
    acct_prof = _account_to_profile(profile_names, prof_acct)
    unit_acct = resolve_unit_accounts(units, config_dir)

    unit_profiles = {}
    unresolved, unmatched = [], []
    for _, rp in units:
        acct = unit_acct.get(rp)
        if acct is None:
            unresolved.append(rp)
        elif acct in acct_prof:
            unit_profiles[rp] = acct_prof[acct]
        else:
            unmatched.append((rp, acct))
    if unresolved:
        print(f"  ⚠ {len(unresolved)} unit(s): no aws_account_id in env.hcl "
              f"— will use default credentials")
    if unmatched:
        accts = sorted({a for _, a in unmatched})
        print(f"  ⚠ {len(unmatched)} unit(s) in unmapped account(s) {accts} "
              f"— no matching profile, using default credentials")
    return unit_profiles, acct_prof


def state_bucket_profile(config_dir, acct_prof, profile_names):
    """Profile that owns the state bucket's account (root.hcl `state_account_id`),
    for the one-shot cross-account state-age listing. Falls back to the first
    profile when the account can't be resolved."""
    if not profile_names:
        return None
    for fn in ("root.hcl", "terragrunt.hcl"):
        p = os.path.join(str(config_dir), fn)
        try:
            m = _STATE_ACCT_RE.search(open(p).read())
        except OSError:
            continue
        if m and m.group(1) in acct_prof:
            return acct_prof[m.group(1)]
    return profile_names[0]
