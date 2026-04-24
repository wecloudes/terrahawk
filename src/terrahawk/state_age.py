"""State age queries: Azure Blob, AWS S3, and GCS backends."""

import json
import re
import shutil
import subprocess


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
    start = rs_match.end()
    depth, j = 1, start
    while j < len(content) and depth > 0:
        if content[j] == "{":
            depth += 1
        elif content[j] == "}":
            depth -= 1
        j += 1
    rs_body = content[start:j - 1]

    # Extract backend type
    backend_match = re.search(r'backend\s*=\s*"([^"]+)"', rs_body)
    backend = backend_match.group(1) if backend_match else ""

    # Extract config { ... } sub-block
    cfg_match = re.search(r'config\s*=\s*\{', rs_body)
    if not cfg_match:
        return backend, rs_body

    cfg_start = cfg_match.end()
    depth, j = 1, cfg_start
    while j < len(rs_body) and depth > 0:
        if rs_body[j] == "{":
            depth += 1
        elif rs_body[j] == "}":
            depth -= 1
        j += 1
    config_body = rs_body[cfg_start:j - 1]
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
    locals_match = re.search(r'locals\s*\{(.*?)\n\}', content, re.DOTALL)
    if locals_match:
        body = locals_match.group(1)
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

        # Determine the prefix in the key template (anything before the
        # first dynamic segment). e.g. "${local.Service}/${path_relative_to_include()}/terraform.tfstate"
        # → need to strip the leading Service value to map back to relative paths.
        key_tpl = key_match.group(1) if key_match else ""
        service_prefix = ""
        tpl_prefix_match = re.match(r'\$\{local\.(\w+)\}/', key_tpl)
        if tpl_prefix_match:
            local_name = tpl_prefix_match.group(1)
            resolved = _resolve_local(local_name, content, config_dir, root_hcl)
            if resolved:
                service_prefix = resolved + "/"

        cmd = ["aws", "s3api", "list-objects-v2",
               "--bucket", bucket_name,
               "--query", "Contents[].{Key:Key,LastModified:LastModified}",
               "--output", "json"]
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
