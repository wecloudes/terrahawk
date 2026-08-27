"""Plan execution worker: find_cache_dir, run_plan."""

import json
import os
import subprocess
import time

from .deps import mise_cmd


def find_cache_dir(unit_dir):
    """Find the .terragrunt-cache working directory with .tf files.

    Walks the cache tree and prefers the deepest directory that contains
    both a terragrunt-generated file (provider.tf or backend.tf) AND at
    least one other .tf file (i.e. the actual module code).
    """
    cache_root = os.path.join(unit_dir, ".terragrunt-cache")
    if not os.path.isdir(cache_root):
        return None

    def score(files):
        tfs = [f for f in files if f.endswith(".tf")]
        if not tfs:
            return 0
        has_generated = "provider.tf" in files or "backend.tf" in files
        has_module = "main.tf" in files or len(tfs) >= 2
        if has_generated and has_module:
            return 3
        if has_generated:
            return 2
        if has_module:
            return 1
        return 0

    best = (0, None)
    for root, dirs, files in os.walk(cache_root):
        if "/examples/" in root or "/test/" in root:
            continue
        s = score(files)
        if s > best[0]:
            best = (s, root)
    return best[1]


def _resolve_timeout(unit_dir, default_timeout, unit_timeouts):
    """Pick a unit's plan timeout: the first `unit_timeouts` pattern that is a
    substring of `unit_dir` wins, otherwise the default."""
    for pattern, t in unit_timeouts.items():
        if pattern in unit_dir:
            return t
    return default_timeout


def _build_plan_args(unit_dir, plan_file, no_hooks=False):
    """Assemble the `terragrunt plan` argument list for one unit.

    `-detailed-exitcode` makes exit 2 mean drift; `-lock=false` and
    `--dependency-fetch-output-from-state` keep the scan read-only and fast.
    `no_hooks` prepends the experimental flags that skip before/after/error
    hooks (requires Terragrunt >=1.0.8 with the experiment enabled).
    """
    plan_args = [
        "plan", "-detailed-exitcode", "-input=false",
        "-lock=false", "-no-color",
        f"-out={plan_file}",
        f"--working-dir={unit_dir}",
        "--non-interactive",
        "--dependency-fetch-output-from-state",
    ]
    if no_hooks:
        plan_args.insert(1, "--experiment=optional-hooks")
        plan_args.insert(2, "--no-hooks")
    return plan_args


def run_plan(unit_dir, rel_path, timeout, args, unit_timeouts, script_dir, tmp_dir, idx):
    """Run terragrunt plan and optional tools for a single unit."""
    result = {
        "unit": rel_path,
        "idx": idx,
    }
    t_start = time.monotonic()

    # Determine per-unit timeout
    my_timeout = _resolve_timeout(unit_dir, timeout, unit_timeouts)

    # Run terragrunt plan
    exit_code = 0
    plan_output = ""
    plan_env = os.environ.copy()
    plan_env["TF_PLUGIN_CACHE_DIR"] = os.path.expanduser("~/.terraform.d/plugin-cache")
    plan_env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "true"

    tf_ver = getattr(args, "terraform_version", "")
    tg_ver = getattr(args, "terragrunt_version", "")

    plan_file = os.path.join(tmp_dir, f"plan_{idx}.tfplan")
    plan_args = _build_plan_args(unit_dir, plan_file, getattr(args, "no_hooks", False))
    plan_cmd = mise_cmd("terragrunt", tg_ver, plan_args)

    retryable_errors = ["Failed to query available provider packages",
                        "Failed to install provider",
                        "plugins are not installed",
                        "Incompatible provider version",
                        "doesn't match any of the checksums",
                        "Failed to load plugin schemas",
                        "failed to instantiate provider",
                        "timeout while waiting for plugin to start"]
    try:
        r = subprocess.run(plan_cmd, capture_output=True, text=True,
                           timeout=my_timeout, env=plan_env)
        exit_code = r.returncode
        plan_output = r.stdout + r.stderr

        # Retry with `init -upgrade` if provider/plugin issues detected —
        # re-downloads the provider plugins to recover from a corrupt/partial
        # cache. This is local-only (provider binaries + .terraform.lock.hcl,
        # which cleanup restores from git); it never touches remote state, so
        # the scan stays read-only.
        if exit_code != 0 and any(e in plan_output for e in retryable_errors):
            subprocess.run(
                mise_cmd("terragrunt", tg_ver, [
                    "init", "-upgrade", "-input=false", "-no-color",
                    f"--working-dir={unit_dir}",
                ]),
                capture_output=True, text=True, timeout=my_timeout, env=plan_env,
            )
            r = subprocess.run(plan_cmd, capture_output=True, text=True,
                               timeout=my_timeout, env=plan_env)
            exit_code = r.returncode
            plan_output = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        exit_code = 124
        plan_output = (e.stdout or b"").decode(errors="replace") + f"\nTIMEOUT: Plan exceeded {my_timeout}s."
    except Exception as e:
        exit_code = 1
        plan_output = str(e)

    result["exit_code"] = exit_code
    result["plan_output"] = plan_output

    # Rendered config (terragrunt 1.x): fully resolved inputs, module source,
    # and remote_state config (no ${local.X} left). Powers exact state-age
    # keys, real input values, and module source without regex parsing.
    try:
        r = subprocess.run(
            mise_cmd("terragrunt", tg_ver, [
                "render", "--format", "json",
                f"--working-dir={unit_dir}",
                "--non-interactive",
                "--dependency-fetch-output-from-state",
            ]),
            capture_output=True, text=True, timeout=60, env=plan_env,
        )
        if r.returncode == 0 and r.stdout.strip():
            result["render_json"] = json.loads(r.stdout)
    except Exception:
        pass  # old terragrunt or render bug — consumers fall back to regex

    # Find cache dir for optional tools
    cache_dir = find_cache_dir(unit_dir)
    result["cache_dir"] = cache_dir

    if not cache_dir:
        result["duration"] = round(time.monotonic() - t_start, 1)
        return result

    # Plan JSON (structured resource changes, out-of-band drift, diagrams).
    # Captured for ANY completed plan: a clean plan (exit 0) can still carry
    # resource_drift entries — changes made outside Terraform with no pending
    # actions — which the text output never surfaces.
    if exit_code in (0, 2) and os.path.exists(plan_file):
        try:
            r = subprocess.run(
                mise_cmd("terraform", tf_ver, [f"-chdir={cache_dir}", "show", "-json", plan_file]),
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                result["plan_json"] = json.loads(r.stdout)
        except Exception:
            pass

    # Terraform show -json (single call for tags + resource count)
    try:
        r = subprocess.run(
            mise_cmd("terraform", tf_ver, [f"-chdir={cache_dir}", "show", "-json"]),
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            state = json.loads(r.stdout)
            result["state_json"] = state
            def count_resources(mod):
                c = len(mod.get("resources", []))
                for child in mod.get("child_modules", []):
                    c += count_resources(child)
                return c
            result["resource_count"] = count_resources(state.get("values", {}).get("root_module", {}))
    except Exception:
        pass

    # Outputs
    try:
        r = subprocess.run(
            mise_cmd("terraform", tf_ver, [f"-chdir={cache_dir}", "output", "-json"]),
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            result["outputs_json"] = json.loads(r.stdout)
    except Exception:
        pass

    # Read local files (no subprocess needed)
    var_file = os.path.join(cache_dir, "variables.tf")
    if os.path.exists(var_file):
        result["variables_tf"] = open(var_file).read()

    # Collect all .tf files that may contain provider/required_providers/default_tags
    # (terragrunt-generated provider.tf + any module-level versions.tf/terraform.tf)
    providers_tf_parts = []
    for fname in ["provider.tf", "providers.tf", "terraform.tf", "versions.tf"]:
        pf = os.path.join(cache_dir, fname)
        if os.path.exists(pf):
            providers_tf_parts.append(open(pf).read())
    if providers_tf_parts:
        result["providers_tf"] = "\n\n".join(providers_tf_parts)

    tg_path = os.path.join(unit_dir, "terragrunt.hcl")
    if os.path.exists(tg_path):
        result["tg_hcl"] = open(tg_path).read()

    result["duration"] = round(time.monotonic() - t_start, 1)
    return result
