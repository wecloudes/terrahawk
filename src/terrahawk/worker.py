"""Plan execution worker: find_cache_dir, run_plan."""

import json
import os
import subprocess
import time

from .deps import mise_cmd, pinned_tf_env, resolve_tf_binary, tf_cmd, version_at_least

# Terragrunt 1.2 graduated the experiments terrahawk relies on: reading
# dependency outputs straight from state is the default (the explicit flag is
# deprecated and errors under strict mode) and `--no-hooks` no longer needs
# `--experiment=optional-hooks` (passing it warns). Older or unknown versions
# keep the explicit flags, which still work everywhere.
TG_GRADUATED = (1, 2, 0)


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


# Provider/plugin failures worth one `init -upgrade` + re-plan. Mostly
# symptoms of a corrupt or partially written provider cache.
_RETRYABLE_INIT_ERRORS = (
    "Failed to query available provider packages",
    "Failed to install provider",
    "plugins are not installed",
    "Incompatible provider version",
    "doesn't match any of the checksums",
    "does not match any of the checksums",  # terraform's actual wording
    "Failed to load plugin schemas",
    "failed to instantiate provider",
    "timeout while waiting for plugin to start",
)


def _is_retryable_init_error(output):
    return any(e in output for e in _RETRYABLE_INIT_ERRORS)


def _resolve_cache_dir(unit_dir, info):
    """The unit's OpenTofu/Terraform working dir.

    Prefers the exact `working_dir` reported by `terragrunt info print` (also
    correct for units without a `terraform { source }`, which run in the unit
    dir itself); falls back to the cache-tree scoring heuristic when info is
    unavailable or the dir holds no .tf files.
    """
    wd = (info or {}).get("working_dir")
    if wd and os.path.isdir(wd) and any(f.endswith(".tf") for f in os.listdir(wd)):
        return wd
    return find_cache_dir(unit_dir)


def _resolve_timeout(unit_dir, default_timeout, unit_timeouts):
    """Pick a unit's plan timeout: the first `unit_timeouts` pattern that is a
    substring of `unit_dir` wins, otherwise the default."""
    for pattern, t in unit_timeouts.items():
        if pattern in unit_dir:
            return t
    return default_timeout


def _fetch_from_state_args(tg_version):
    """`--dependency-fetch-output-from-state` for pre-1.2 Terragrunt only."""
    if version_at_least(tg_version, TG_GRADUATED):
        return []
    return ["--dependency-fetch-output-from-state"]


def _info_args(unit_dir, tg_version):
    """`terragrunt info print` for one unit (working dir + binary).

    Dependency outputs are skipped — they aren't needed here and would cost a
    state read per dependency. Below 1.2 that flag sits behind the
    `optional-dependency-outputs` experiment; if this terragrunt lacks it the
    call fails and the caller falls back to the heuristic.
    """
    info_args = ["info", "print", f"--working-dir={unit_dir}", "--non-interactive",
                 "--no-dependency-outputs"]
    if not version_at_least(tg_version, TG_GRADUATED):
        info_args.insert(2, "--experiment=optional-dependency-outputs")
    return info_args


def uses_provider_cache(env, tg_version, enabled=True):
    """True when Terragrunt's provider cache server should be used (see
    `_provider_cache_env`)."""
    if env.get("TG_PROVIDER_CACHE", "1").lower() in ("0", "false"):
        return False
    return enabled and version_at_least(tg_version, (1, 0, 0))


def _provider_cache_env(env, tg_version, enabled=True):
    """Configure provider caching for the parallel per-unit plans. Mutates env.

    Terraform's shared `plugin_cache_dir` is not safe for concurrent `init`:
    on a cold cache (every fresh CI container) parallel units fail with
    "Required plugins are not installed" and only recover through the
    `init -upgrade` retry. Terragrunt's provider cache server serialises
    downloads across processes with per-provider lock files, so it is the
    default on Terragrunt 1.x. The plugin dir remains the fallback when
    disabled (`--no-provider-cache`, or `TG_PROVIDER_CACHE=0` in the env) or
    when the Terragrunt version is older/unknown.
    """
    if uses_provider_cache(env, tg_version, enabled):
        env.setdefault("TG_PROVIDER_CACHE", "1")
        # Terragrunt clears this for terraform anyway; drop it so the env is
        # unambiguous (the Docker image sets it globally).
        env.pop("TF_PLUGIN_CACHE_DIR", None)
        env.pop("TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE", None)
    else:
        env["TF_PLUGIN_CACHE_DIR"] = os.path.expanduser("~/.terraform.d/plugin-cache")
        env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "true"
    return env


def _build_plan_args(unit_dir, plan_file, no_hooks=False, tg_version=None):
    """Assemble the `terragrunt plan` argument list for one unit.

    `-detailed-exitcode` makes exit 2 mean drift; `-lock=false` and reading
    dependency outputs from state keep the scan read-only and fast.
    `no_hooks` inserts `--no-hooks` to skip before/after/error hooks
    (Terragrunt >=1.0.8; below 1.2 it also needs the `optional-hooks`
    experiment). *tg_version* is the parsed Terragrunt version tuple.
    """
    plan_args = [
        "plan", "-detailed-exitcode", "-input=false",
        "-lock=false", "-no-color",
        f"-out={plan_file}",
        f"--working-dir={unit_dir}",
        "--non-interactive",
    ] + _fetch_from_state_args(tg_version)
    if no_hooks:
        hooks = ["--no-hooks"]
        if not version_at_least(tg_version, TG_GRADUATED):
            hooks.insert(0, "--experiment=optional-hooks")
        plan_args[1:1] = hooks
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
    tg_version = getattr(args, "tg_version", None)
    plan_env = _provider_cache_env(os.environ.copy(), tg_version,
                                   not getattr(args, "no_provider_cache", False))
    # Multi-account: use the AWS profile mapped to this unit's account so plan,
    # init retry, and render all run with the right credentials.
    unit_profile = (getattr(args, "unit_profiles", None) or {}).get(rel_path)
    if unit_profile:
        plan_env["AWS_PROFILE"] = unit_profile

    tf_ver = getattr(args, "terraform_version", "")
    tg_ver = getattr(args, "terragrunt_version", "")
    # A pinned Terraform must be what terragrunt itself runs, not just what
    # terrahawk's own show/output calls use: put it on the mise PATH and
    # select it via TG_TF_PATH.
    pinned_tf_env(args, plan_env)
    tf_pin = [f"terraform@{tf_ver}"] if tf_ver else []

    def tg_cmd(tool_args):
        return mise_cmd("terragrunt", tg_ver, tool_args, extra=tf_pin)

    plan_file = os.path.join(tmp_dir, f"plan_{idx}.tfplan")
    plan_args = _build_plan_args(unit_dir, plan_file, getattr(args, "no_hooks", False), tg_version)
    plan_cmd = tg_cmd(plan_args)

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
        if exit_code != 0 and _is_retryable_init_error(plan_output):
            subprocess.run(
                tg_cmd([
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
            tg_cmd([
                "render", "--format", "json",
                f"--working-dir={unit_dir}",
                "--non-interactive",
            ] + _fetch_from_state_args(tg_version)),
            capture_output=True, text=True, timeout=60, env=plan_env,
        )
        if r.returncode == 0 and r.stdout.strip():
            result["render_json"] = json.loads(r.stdout)
    except Exception:
        pass  # old terragrunt or render bug — consumers fall back to regex

    # Terragrunt's own view of the unit: exact working dir and the
    # OpenTofu/Terraform binary it resolved. Falls back to heuristics.
    unit_info = {}
    try:
        r = subprocess.run(tg_cmd(_info_args(unit_dir, tg_version)),
                           capture_output=True, text=True, timeout=60, env=plan_env)
        if r.returncode == 0 and r.stdout.strip():
            unit_info = json.loads(r.stdout)
    except Exception:
        pass  # older terragrunt / experiment unavailable — heuristic fallback

    cache_dir = _resolve_cache_dir(unit_dir, unit_info)
    result["cache_dir"] = cache_dir

    if not cache_dir:
        result["duration"] = round(time.monotonic() - t_start, 1)
        return result

    # Follow-up show/output calls must use the same binary that wrote the
    # plan file and initialised cache_dir (terragrunt prefers tofu when it is
    # installed), and the same env — `show`/`output` read remote state, so a
    # multi-account unit needs its AWS_PROFILE here too.
    tf_bin = unit_info.get("terraform_binary") or resolve_tf_binary(result.get("render_json"), plan_env)

    # Plan JSON (structured resource changes, out-of-band drift, diagrams).
    # Captured for ANY completed plan: a clean plan (exit 0) can still carry
    # resource_drift entries — changes made outside Terraform with no pending
    # actions — which the text output never surfaces.
    if exit_code in (0, 2) and os.path.exists(plan_file):
        try:
            r = subprocess.run(
                tf_cmd(tf_bin, tf_ver, [f"-chdir={cache_dir}", "show", "-json", plan_file]),
                capture_output=True, text=True, timeout=30, env=plan_env,
            )
            if r.returncode == 0 and r.stdout.strip():
                result["plan_json"] = json.loads(r.stdout)
        except Exception:
            pass

    # Terraform show -json (single call for tags + resource count)
    try:
        r = subprocess.run(
            tf_cmd(tf_bin, tf_ver, [f"-chdir={cache_dir}", "show", "-json"]),
            capture_output=True, text=True, timeout=30, env=plan_env,
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
            tf_cmd(tf_bin, tf_ver, [f"-chdir={cache_dir}", "output", "-json"]),
            capture_output=True, text=True, timeout=30, env=plan_env,
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
