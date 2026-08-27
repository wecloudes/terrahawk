"""State age queries: Azure Blob, AWS S3, and GCS backends."""

import json
import os
import re
import shutil
import subprocess


# Environment variables that supply an explicit Azure Storage credential. When
# any is set, `az storage blob list` uses key/SAS auth and we must not force
# AD token auth.
_AZURE_KEY_ENV_VARS = (
    "AZURE_STORAGE_KEY",
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_CONNECTION_STRING",
    "AZURE_STORAGE_SAS_TOKEN",
)


def _azure_auth_args(env):
    """Choose the `az storage` auth mode for the given environment.

    Returns `["--auth-mode", "login"]` to use the AD token from a CI
    `az login` (service principal + blob-data RBAC), which does not require
    storage-account-key access — the recommended, locked-down path. Returns
    `[]` (az's default key/SAS auth) when an explicit account key, connection
    string, or SAS token is present in the environment, so existing key-based
    setups keep working unchanged.
    """
    if any(env.get(v) for v in _AZURE_KEY_ENV_VARS):
        return []
    return ["--auth-mode", "login"]


def _brace_block(text, start):
    """Return the body of a brace block and the index just past its close.

    `start` must be the index immediately after the opening `{`. Walks braces
    tracking depth and returns (body, end) where body is the text between the
    braces (exclusive) and end is the index just past the matching `}`. Not
    string-aware — HCL block bodies rarely carry literal braces inside strings.
    """
    depth, j = 1, start
    while j < len(text) and depth > 0:
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    # Exclude the closing brace only if we actually consumed one; on
    # unbalanced input we walked to the end and there is nothing to trim.
    body_end = j - 1 if depth == 0 else j
    return text[start:body_end], j


def _parse_hcl_string_locals(block_text):
    """Extract simple `name = "value"` assignments from an HCL block body."""
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', block_text))


def _normalize_iso(ts):
    """Normalise `+HHMM` timezone offsets to `+HH:MM` for datetime.fromisoformat()."""
    m = re.match(r'^(.*)([+-])(\d{2})(\d{2})$', ts)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4)}"
    return ts


def _extract_remote_state_config(content):
    """Extract the config block from `remote_state { ... config = { ... } }`.

    Returns a tuple (backend, config_body) where config_body is the text
    inside the config block braces.  Falls back to the full file content
    when the block can't be located so the old regex-on-full-file behaviour
    is preserved.
    """
    # Find remote_state { ... }
    rs_match = re.search(r'remote_state\s*\{', content)
    if not rs_match:
        # No remote_state block — fall back to whole file
        backend_match = re.search(r'backend\s*=\s*"([^"]+)"', content)
        return (backend_match.group(1) if backend_match else ""), content

    # Walk braces to find the whole remote_state block
    rs_body, _ = _brace_block(content, rs_match.end())

    # Extract backend type
    backend_match = re.search(r'backend\s*=\s*"([^"]+)"', rs_body)
    backend = backend_match.group(1) if backend_match else ""

    # Extract config { ... } sub-block
    cfg_match = re.search(r'config\s*=\s*\{', rs_body)
    if not cfg_match:
        return backend, rs_body

    config_body, _ = _brace_block(rs_body, cfg_match.end())
    return backend, config_body


def _query_gcs_blob_dates(config_dir, content):
    """Query GCS for state file update times.

    The root HCL typically derives bucket/prefix from locals that reference
    per-environment env.hcl files (e.g. `read_terragrunt_config(find_in_parent_folders("env.hcl"))`).
    We discover every env.hcl under the config dir, resolve the bucket and
    prefix templates with each env's locals, then list state objects.
    """
    blob_dates = {}
    if not shutil.which("gcloud"):
        return blob_dates

    bucket_tpl_match = re.search(r'bucket\s*=\s*"([^"]+)"', content)
    prefix_tpl_match = re.search(r'prefix\s*=\s*"([^"]+)"', content)
    if not bucket_tpl_match:
        return blob_dates
    bucket_tpl = bucket_tpl_match.group(1)
    prefix_tpl = prefix_tpl_match.group(1) if prefix_tpl_match else ""

    # Parse the root `locals { ... }` block.
    root_locals = {}
    # Bare references like `env = local.env_vars.locals.environment`
    root_local_refs = {}
    # Brace-walk the block rather than matching to the first `\n}` — a nested
    # map/object value (e.g. `default_tags = { ... }`) would otherwise truncate
    # the body and drop later locals.
    locals_match = re.search(r'locals\s*\{', content)
    if locals_match:
        body, _ = _brace_block(content, locals_match.end())
        root_locals = _parse_hcl_string_locals(body)
        for name, ref in re.findall(
            r'(\w+)\s*=\s*local\.env_vars\.locals\.(\w+)', body,
        ):
            root_local_refs[name] = ref

    # Discover env.hcl files under the config dir. If none, use a single
    # empty context so a static bucket template still resolves.
    env_files = sorted(config_dir.glob("*/env.hcl"))
    env_contexts = []
    for env_file in env_files:
        env_contexts.append(_parse_hcl_string_locals(env_file.read_text()))
    if not env_contexts:
        env_contexts.append({})

    def _resolve(tpl, env_locals):
        """Expand ${local.X} and ${local.env_vars.locals.X} references,
        iterating until stable (handles composite locals that reference
        other locals)."""
        pool = dict(root_locals)
        # Seed bare `local = local.env_vars.locals.<k>` assignments
        for name, ref in root_local_refs.items():
            if ref in env_locals:
                pool[name] = env_locals[ref]

        def _sub_once(s):
            s = re.sub(
                r'\$\{local\.env_vars\.locals\.(\w+)\}',
                lambda m: env_locals.get(m.group(1), m.group(0)),
                s,
            )
            s = re.sub(
                r'\$\{local\.(\w+)\}',
                lambda m: pool.get(m.group(1), m.group(0)),
                s,
            )
            return s

        # Stabilise the pool (composite locals referencing other locals)
        for _ in range(10):
            changed = False
            for k, v in list(pool.items()):
                nv = _sub_once(v)
                if nv != v:
                    pool[k] = nv
                    changed = True
            if not changed:
                break

        # Stabilise the template itself
        out = tpl
        for _ in range(10):
            nout = _sub_once(out)
            if nout == out:
                break
            out = nout
        return out

    for env_locals in env_contexts:
        bucket = _resolve(bucket_tpl, env_locals)
        prefix_resolved = _resolve(prefix_tpl, env_locals)
        # Strip the dynamic ${path_relative_to_include()} and anything after it
        prefix_static = re.sub(
            r'\$\{path_relative_to_include\(\)\}.*$', '', prefix_resolved,
        )
        # Skip if any interpolation remains unresolved
        if "${" in bucket or "${" in prefix_static:
            continue

        url = f"gs://{bucket}/{prefix_static}**"
        try:
            r = subprocess.run(
                ["gcloud", "storage", "objects", "list", url,
                 "--format=value(name,update_time)"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            continue
        if r.returncode != 0:
            continue

        # Bucket may have object versioning enabled — keep the latest update
        # per object name (live version has the most recent update_time).
        latest = {}
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name, updated = parts[0], parts[1]
            if not name.endswith("/default.tfstate"):
                continue
            prev = latest.get(name)
            if prev is None or updated > prev:
                latest[name] = updated

        for name, updated in latest.items():
            rel_key = name
            if prefix_static and rel_key.startswith(prefix_static):
                rel_key = rel_key[len(prefix_static):]
            # The lookup key in assemble_unit_result uses "/terraform.tfstate"
            if rel_key.endswith("/default.tfstate"):
                rel_key = rel_key[:-len("/default.tfstate")] + "/terraform.tfstate"
            blob_dates[rel_key] = _normalize_iso(updated)

    return blob_dates


def extract_root_provider_template(config_dir):
    """Extract the generated provider.tf template from the root terragrunt.hcl.

    Returns the contents of the `generate "provider"` block (the literal HCL
    that gets written to provider.tf in each unit), or "" if not found.
    Used as a fallback so units without a populated cache can still report
    their required_providers and default_tags template.
    """
    for candidate in ("root.hcl", "terragrunt.hcl"):
        p = config_dir / candidate
        if not p.exists():
            continue
        text = p.read_text()
        # Match: generate "provider" { ... contents = <<EOF ... EOF ... }
        m = re.search(
            r'generate\s+"provider"\s*\{[^}]*?contents\s*=\s*<<-?(\w+)\s*\n(.*?)\n\s*\1',
            text, re.DOTALL,
        )
        if m:
            return m.group(2)
    return ""


def _resolve_s3_static_prefix(key_tpl, root_hcl_content, config_dir, root_hcl):
    """Resolve the static leading portion of an S3 key template to a concrete prefix.

    A state key is `<static_prefix><path_relative_to_include()>/terraform.tfstate`,
    and `path_relative_to_include()` equals the unit's rel_path — so stripping
    the static prefix from a listed S3 key maps it back to the rel_path-based
    lookup key. The static prefix is everything before the dynamic
    `${path_relative_to_include()}` segment; it may contain literal path
    segments AND any number of `${local.X}` references (e.g.
    `env/${local.Env}/${local.Service}/${path_relative_to_include()}/...`).

    Returns the resolved prefix (with trailing content preserved as written),
    or "" when there is no static prefix, no dynamic segment, or an
    interpolation can't be resolved (bail rather than mis-map keys).
    """
    marker = "${path_relative_to_include()}"
    if marker not in key_tpl:
        return ""
    head = key_tpl.split(marker, 1)[0]
    if not head:
        return ""

    def _sub(m):
        val = _resolve_local(m.group(1), root_hcl_content, config_dir, root_hcl)
        return val if val else m.group(0)

    resolved = re.sub(r'\$\{local\.(\w+)\}', _sub, head)
    if "${" in resolved:  # unresolved local or other interpolation — unsafe to strip
        return ""
    return resolved


def _resolve_local(local_name, root_hcl_content, config_dir, root_hcl):
    """Resolve a Terragrunt local to its string value.

    Searches the root HCL content first, then sibling HCL files (e.g.
    global.hcl, customer.hcl) for a `local_name = "value"` assignment.
    Returns the resolved value or "" if not found.
    """
    search_texts = [root_hcl_content]
    try:
        for hcl in config_dir.glob("*.hcl"):
            if hcl != root_hcl:
                search_texts.append(hcl.read_text())
    except Exception:
        pass
    for txt in search_texts:
        for val_match in re.finditer(
            r'%s\s*=\s*"([^"]+)"' % re.escape(local_name), txt,
        ):
            val = val_match.group(1)
            if "${" not in val:
                return val
    return ""


def query_blob_dates(config_dir):
    """Query remote state backend for state file last-modified dates.

    Supports Azure Blob Storage, AWS S3 and Google Cloud Storage backends.
    Reads the backend config from the first terragrunt.hcl / root.hcl found
    in the config dir. Returns {relative_key: iso8601_datetime}, where
    relative_key matches the unit's relative path with '/terraform.tfstate'
    suffix.
    """
    blob_dates = {}
    root_hcl = None
    for candidate in ("root.hcl", "terragrunt.hcl"):
        p = config_dir / candidate
        if p.exists():
            root_hcl = p
            break
    if not root_hcl:
        print("  \u26a0\ufe0f  No root.hcl or terragrunt.hcl found — skipping state age query.")
        return blob_dates

    content = root_hcl.read_text()

    # Extract the remote_state config block so regex matches are scoped
    # to the backend configuration, not the entire file (avoids matching
    # provider or generate block values).
    backend, config_block = _extract_remote_state_config(content)

    # ── Google Cloud Storage ───────────────────────────────────
    if backend == "gcs":
        return _query_gcs_blob_dates(config_dir, content)

    # ── Azure Blob Storage ─────────────────────────────────────
    if backend == "azurerm" or "storage_account_name" in config_block:
        if not shutil.which("az"):
            print("  \u26a0\ufe0f  az CLI not found — skipping Azure state age query.")
            return blob_dates

        sa_match = re.search(r'storage_account_name\s*=\s*"([^"]+)"', config_block)
        ct_match = re.search(r'container_name\s*=\s*"([^"]+)"', config_block)

        # Resolve locals if the values use interpolation
        sa_name = sa_match.group(1) if sa_match else ""
        ct_name = ct_match.group(1) if ct_match else ""

        if not sa_name or not ct_name:
            print("  \u26a0\ufe0f  Could not parse storage_account_name or container_name from remote_state config.")
            return blob_dates

        # Resolve ${local.X} interpolations in storage account / container
        if "${" in sa_name:
            for m in re.finditer(r'\$\{local\.(\w+)\}', sa_name):
                resolved = _resolve_local(m.group(1), content, config_dir, root_hcl)
                if resolved:
                    sa_name = sa_name.replace(m.group(0), resolved)
        if "${" in ct_name:
            for m in re.finditer(r'\$\{local\.(\w+)\}', ct_name):
                resolved = _resolve_local(m.group(1), content, config_dir, root_hcl)
                if resolved:
                    ct_name = ct_name.replace(m.group(0), resolved)

        if "${" in sa_name or "${" in ct_name:
            print(f"  \u26a0\ufe0f  Could not resolve Azure backend config — storage_account_name={sa_name}, container_name={ct_name}")
            return blob_dates

        try:
            r = subprocess.run(
                ["az", "storage", "blob", "list",
                 "--account-name", sa_name,
                 "--container-name", ct_name,
                 # Use AD token auth for CI service principals unless an explicit
                 # storage credential is in the environment (see _azure_auth_args).
                 *_azure_auth_args(os.environ),
                 # Default --num-results caps at 5000 blobs and silently drops
                 # the rest; "*" follows continuation tokens and returns all.
                 "--num-results", "*",
                 "--query", "[?ends_with(name, 'terraform.tfstate')].{name:name, lastModified:properties.lastModified}",
                 "-o", "json"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout.strip():
                for b in json.loads(r.stdout):
                    blob_dates[b["name"]] = b["lastModified"]
            elif r.returncode != 0:
                print(f"  \u26a0\ufe0f  az storage blob list failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            print("  \u26a0\ufe0f  az storage blob list timed out.")
        except Exception as e:
            print(f"  \u26a0\ufe0f  Azure state age query error: {e}")
        return blob_dates

    # ── AWS S3 ─────────────────────────────────────────────────
    if backend == "s3" or "bucket" in config_block:
        if not shutil.which("aws"):
            print("  \u26a0\ufe0f  aws CLI not found — skipping S3 state age query.")
            return blob_dates

        bucket_match = re.search(r'bucket\s*=\s*"([^"]+)"', config_block)
        profile_match = re.search(r'profile\s*=\s*"([^"]+)"', config_block)
        region_match = re.search(r'region\s*=\s*"([^"]+)"', config_block)
        key_match = re.search(r'key\s*=\s*"([^"]+)"', config_block)

        bucket_name = bucket_match.group(1) if bucket_match else ""

        if not bucket_name:
            print("  \u26a0\ufe0f  Could not parse bucket from remote_state config.")
            return blob_dates

        # Resolve ${local.X} in bucket name
        if "${" in bucket_name:
            for m in re.finditer(r'\$\{local\.(\w+)\}', bucket_name):
                resolved = _resolve_local(m.group(1), content, config_dir, root_hcl)
                if resolved:
                    bucket_name = bucket_name.replace(m.group(0), resolved)
            if "${" in bucket_name:
                print(f"  \u26a0\ufe0f  Could not resolve S3 bucket name: {bucket_name}")
                return blob_dates

        # Resolve profile and region (only use if they're concrete strings)
        profile = profile_match.group(1) if profile_match else ""
        region = region_match.group(1) if region_match else ""
        if "${" in profile:
            resolved = _resolve_local(
                re.search(r'\$\{local\.(\w+)\}', profile).group(1),
                content, config_dir, root_hcl
            ) if re.search(r'\$\{local\.(\w+)\}', profile) else ""
            profile = resolved if resolved else ""
        if "${" in region:
            resolved = _resolve_local(
                re.search(r'\$\{local\.(\w+)\}', region).group(1),
                content, config_dir, root_hcl
            ) if re.search(r'\$\{local\.(\w+)\}', region) else ""
            region = resolved if resolved else ""

        # Determine the static prefix in the key template (everything before
        # ${path_relative_to_include()}) so listed keys map back to rel_paths.
        # Handles literal segments and multiple ${local.X} refs, e.g.
        # "env/${local.Service}/${path_relative_to_include()}/terraform.tfstate".
        key_tpl = key_match.group(1) if key_match else ""
        service_prefix = _resolve_s3_static_prefix(key_tpl, content, config_dir, root_hcl)

        # `aws s3api list-objects-v2` is a paginated operation: the AWS CLI
        # auto-follows the continuation token and aggregates ALL objects, so
        # there is no 1000-key truncation. When we know the service prefix,
        # pass it as --prefix to list only the relevant subtree (faster and
        # cheaper on large shared state buckets).
        cmd = ["aws", "s3api", "list-objects-v2",
               "--bucket", bucket_name,
               "--query", "Contents[].{Key:Key,LastModified:LastModified}",
               "--output", "json"]
        if service_prefix:
            cmd += ["--prefix", service_prefix]
        if profile:
            cmd += ["--profile", profile]
        if region:
            cmd += ["--region", region]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                for obj in json.loads(r.stdout) or []:
                    k = obj.get("Key", "")
                    if service_prefix and k.startswith(service_prefix):
                        k = k[len(service_prefix):]
                    blob_dates[k] = obj.get("LastModified")
            elif r.returncode != 0:
                print(f"  \u26a0\ufe0f  aws s3api list-objects-v2 failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            print("  \u26a0\ufe0f  aws s3api list-objects-v2 timed out.")
        except Exception as e:
            print(f"  \u26a0\ufe0f  S3 state age query error: {e}")
    return blob_dates
