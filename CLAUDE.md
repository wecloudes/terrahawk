# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Terrahawk is a **Terragrunt infrastructure scanning tool** that runs `terragrunt plan` across all units in parallel and generates a fully static, self-contained HTML report combining drift detection, architecture diagrams, module introspection, resource tagging, and state age tracking.

It is designed to run on a **daily schedule via CI/CD** (GitHub Actions, GitLab CI, Azure DevOps, Jenkins). The generated report is published to static storage (S3, Azure Blob, GCS) and viewed in the browser — no backend required.

## Supported Clouds

| Cloud | State Backend | State Age CLI | Docker Variant | Credential Mount |
|-------|--------------|---------------|----------------|------------------|
| AWS | S3 | `aws s3api list-objects-v2` | `--build-arg CLOUD=aws` | `$HOME/.aws` |
| Azure | Azure Blob Storage | `az storage blob list` | `--build-arg CLOUD=azure` | `$HOME/.azure` |
| GCP | Google Cloud Storage | `gcloud storage objects list` | `--build-arg CLOUD=gcp` | `$HOME/.config/gcloud` |

Each Docker variant ships **only** the CLI needed for its backend. The Python code auto-detects the backend type from the `remote_state { backend = "..." }` block in the root HCL file.

## Requirements

### Runtime Tools

| Tool | Required | Purpose |
|------|----------|---------|
| Python 3.9+ | Yes | Terrahawk runtime (stdlib only, no pip dependencies) |
| [Terraform](https://github.com/hashicorp/terraform) / [OpenTofu](https://opentofu.org/) | Yes | `terraform plan`, `terraform show -json`, `terraform output -json` |
| [Terragrunt](https://github.com/gruntwork-io/terragrunt) | Yes | `terragrunt plan`, `terragrunt init` |
| [mise](https://mise.jdx.dev) | No | Version pinning for Terraform/Terragrunt (required when `terraform_version` or `terragrunt_version` is set) |
| [AWS CLI](https://github.com/aws/aws-cli) | No | State age queries (S3 backend) |
| [Azure CLI](https://github.com/Azure/azure-cli) | No | State age queries (Azure Blob backend) |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | No | State age queries (GCS backend) |
| git | No | Restoring `.terraform.lock.hcl` files after scan |
| ssh | No | Cloning Terraform modules from private Git repos over SSH |

### Expected Repository Layout

Terrahawk expects a Terragrunt mono-repo structure. `detect_config_dir()` in `config.py` searches for the config root in this order:

1. `{root_dir}/terragrunt/config/` — if it contains `root.hcl` or `terragrunt.hcl`
2. `{root_dir}/terragrunt/` — if it contains `root.hcl` or `terragrunt.hcl`
3. `{root_dir}/config/` — if it contains `root.hcl` or `terragrunt.hcl`
4. `{root_dir}/` — if it contains `root.hcl` or `terragrunt.hcl`
5. **Fallback**: finds the common ancestor of all `terragrunt.hcl` files in the tree

### Unit Discovery

A **unit** is any subdirectory of `config_dir` that contains a `terragrunt.hcl` file. Discovery (`discovery.py`) works as follows:

- **Native path (preferred)**: runs `terragrunt find --format json --dependencies` in `config_dir`. Terragrunt's own HCL parser returns units plus exact dependency lists (catches `dependencies { paths }` blocks, include-based deps, and stacks). `discover_units()` returns `(units, deps)` where `deps` maps `unit_dir -> set(dependency_unit_dirs)`.
- **Fallback path**: when `terragrunt find` fails (older terragrunt, parse error), falls back to `config_dir.rglob("terragrunt.hcl")` and returns `deps=None`; `build_dag()` then re-parses `dependency` blocks with a regex (which misses `dependencies { paths }` blocks).
- Skips any path containing `.terragrunt-cache`
- Skips the root `config_dir` itself (the root `terragrunt.hcl` / `root.hcl` is config, not a unit)
- Unit dirs are `os.path.realpath`-resolved so dependency paths match across symlinks
- Each unit's `rel_path` is relative to `config_dir` (e.g., `landingzones/production/westeurope/app-gateway`)
- The `--exclude` regex is matched against `rel_path`
- The `--unit` flag filters to a single unit by exact `rel_path` match or suffix match (e.g., `--unit app-gateway` matches `production/westeurope/app-gateway`)

### Per-Unit Data Collection

After `terragrunt plan`, the worker (`worker.py`) collects additional data from the `.terragrunt-cache` working directory:

| Data | Source | Used For |
|------|--------|----------|
| Provider requirements | `provider.tf`, `providers.tf`, `terraform.tf`, `versions.tf` in cache dir | Provider table in module info |
| Input variables | `variables.tf` in cache dir | Inputs table in module info |
| Resource tags | `terraform show -json` (state JSON) | Tag extraction and filtering |
| Resource count | `terraform show -json` (recursive module count) | Resource count badge |
| Outputs | `terraform output -json` | Outputs table in module info |
| Module source | `source = "..."` in unit's `terragrunt.hcl` | Module source display |
| Architecture diagram | Built from `plan_json` + `state_json` (if `--diagrams`) | Mermaid diagram |
| Plan JSON | `terraform show -json <planfile>` (captured on exit 0 AND 2) | Structured `resource_changes` (fallback when text parse fails) + `resource_drift` |
| Out-of-band drift | `plan_json.resource_drift` | `driftedResources` entry field → "⚠ N ext" badge (HTML), "Changed Outside Terraform" section (TUI). Surfaces even on clean plans |
| Unit duration | `time.monotonic()` around plan + collection | `duration` field, console `(Ns)` suffix |
| Rendered config | `terragrunt render --format json` (per unit, after plan) | Resolved module source, resolved input values (secret-named inputs masked), exact remote_state key for state age. Regex fallbacks remain for old terragrunt |

### Provider Extraction

Provider information is extracted from **four specific `.tf` files** in the `.terragrunt-cache` working directory, concatenated in order:

1. `provider.tf` — typically generated by Terragrunt's `generate "provider"` block
2. `providers.tf` — alternative naming convention
3. `terraform.tf` — common for `required_providers` blocks
4. `versions.tf` — common for version constraints

The parser extracts:
- `required_version` constraint (Terraform/OpenTofu version)
- `required_providers { ... }` blocks (provider name, source, version)
- Legacy `provider "X" { version = "..." }` blocks (deprecated but still valid)

When a unit's cache directory is empty or incomplete, the **root provider template** is used as fallback. This is extracted from the root HCL's `generate "provider" { contents = <<EOF ... EOF }` block by `extract_root_provider_template()` in `state_age.py`.

### Cache Directory Detection

The `.terragrunt-cache` tree can contain multiple directories with `.tf` files (the actual module, examples, tests, submodules). `find_cache_dir()` in `worker.py` scores directories by:

| Score | Criteria |
|-------|----------|
| 3 | Has a generated file (`provider.tf` or `backend.tf`) AND module code (`main.tf` or 2+ `.tf` files) |
| 2 | Has a generated file only |
| 1 | Has module code only |
| 0 | No `.tf` files |

Directories under `/examples/` or `/test/` are excluded. The highest-scoring directory is selected.

## Build & Development Commands

```bash
# Run directly from repo root
python3 terrahawk.py --help
python3 terrahawk.py --version

# Run as Python module
python3 -m terrahawk --help

# View results in terminal (latest report)
python3 terrahawk.py view

# View a specific report
python3 terrahawk.py view terrahawk_20260511_060000

# Print repo-level unit dependency graph as Mermaid (or write HTML with -o)
python3 terrahawk.py graph [-r DIR] [-o graph.html]

# Instant unit inventory + last-report status (no scan)
python3 terrahawk.py list [-r DIR]

# Scan only units affected by git changes since main (CI-friendly)
python3 terrahawk.py --affected main

# Install as editable package
pip install -e .
terrahawk --help

# Verify all modules import cleanly
python3 -c "
import sys; sys.path.insert(0, 'src')
from terrahawk import cli, config, deps, discovery, incremental
from terrahawk import worker, plan_parser, process, state_age, report, tui
print('All modules import successfully')
"

# Build Docker images (one per cloud backend)
docker build --build-arg CLOUD=aws   -t terrahawk:aws   .
docker build --build-arg CLOUD=azure -t terrahawk:azure .
docker build --build-arg CLOUD=gcp   -t terrahawk:gcp   .

# Run via Docker (example: AWS)
docker run --rm \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
  -v "$HOME/.aws":/home/nonroot/.aws \
  -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
  terrahawk:aws --root-dir /workspace
```

## Architecture

### Package Structure

```
terrahawk/
├── terrahawk.py                 # Thin shim (sys.path + from terrahawk.cli import main)
├── pyproject.toml               # Package metadata, entry point, version
├── Dockerfile                   # Multi-stage, per-cloud variant (aws/azure/gcp)
├── src/terrahawk/
│   ├── __init__.py              # __version__ (keep in sync with pyproject.toml), re-export main
│   ├── __main__.py              # python -m terrahawk support
│   ├── cli.py                   # main() split into phase functions + orchestration
│   ├── config.py                # load_config, load_unit_timeouts, detect_config_dir
│   ├── deps.py                  # TOOLS dict, check_dependencies, get_versions
│   ├── discovery.py             # discover_units, build_dag
│   ├── incremental.py           # load_manifest, hash_file, save_manifest
│   ├── worker.py                # find_cache_dir, run_plan
│   ├── plan_parser.py           # parse_plan_resources + helpers
│   ├── process.py               # process_result orchestrator + sub-processors
│   ├── state_age.py             # query_blob_dates (Azure/AWS/GCS), extract_root_provider_template
│   ├── report.py                # get_html_template, generate_report
│   ├── graph.py                 # terragrunt dag graph → Mermaid (terrahawk graph)
│   ├── hygiene.py               # hcl validate + hcl format --check (per-unit annotations)
│   ├── tui.py                   # Terminal UI viewer (curses-based)
│   └── templates/
│       ├── report.html          # Self-contained HTML report template
│       ├── eagle.svg            # Logo (light theme)
│       └── eagle-white.svg      # Logo (dark theme)
├── THIRD_PARTY_LICENSES         # Licenses for bundled/invoked tools
├── CONTRIBUTING.md
├── CHANGELOG.md
└── LICENSE                      # MIT
```

### Import Graph (strict DAG, no cycles)

```
cli.py ──→ config.py, deps.py, discovery.py, incremental.py,
           worker.py, process.py, state_age.py, report.py, tui.py
worker.py ──→ deps.py (mise_cmd helper)
discovery.py ──→ deps.py (mise_cmd helper)
graph.py ──→ deps.py (mise_cmd helper)
hygiene.py ──→ deps.py (mise_cmd helper)
process.py ──→ plan_parser.py
(all other modules: only stdlib)
```

When adding new modules, **never introduce import cycles**. Leaf modules (deps, config, incremental, plan_parser, state_age, report) must only import from the standard library. `worker.py` and `discovery.py` may import from `deps.py` for the `mise_cmd` helper.

### Module Responsibilities

| Module | What it does |
|--------|-------------|
| `cli.py` | Main pipeline: parse args → clean cache → setup → discover → incremental → query state ages → build DAG → execute plans → assemble results → write outputs → cleanup |
| `config.py` | Loads `.terrahawk.yml` (simple YAML parser, no pyyaml dependency), detects config directory by walking common Terragrunt layouts |
| `deps.py` | Checks required tools (terraform, terragrunt) and optional tools (az, aws, gcloud), captures versions. Provides `mise_cmd()` helper to wrap commands with `mise exec tool@version --` when version pinning is active |
| `discovery.py` | Finds units via `terragrunt find --json --dependencies` (rglob fallback), builds dependency DAG via Kahn's algorithm |
| `incremental.py` | MD5-based manifest for incremental scanning |
| `worker.py` | Runs `terragrunt plan` + terraform show/output per unit |
| `plan_parser.py` | Extracts structured resource changes from `terraform plan` text output |
| `process.py` | Transforms raw worker results into report entries. Sub-processors: `_classify_status`, `_process_diagram`, `_process_tags`, `_process_outputs`, `_process_inputs`, `_process_providers`, `_process_module_source`, `_compute_state_age` |
| `state_age.py` | Queries remote state backends for last-modified dates. Scoped to `remote_state.config` block to avoid matching provider block values. Resolves `${local.X}` interpolations via sibling HCL files |
| `report.py` | Writes a companion `_data.js` file and generates the HTML report that loads it via `<script src>`. Config flags injected via `%%PLACEHOLDER%%` substitution |
| `tui.py` | Curses-based terminal viewer for JSON reports. Launched via `terrahawk view [report]`. Modes: list (grouped by env/sub, coverage bar, status/sub/tag/search filters, sort), detail (scrollable diffs with wrap toggle), plan (expandable per-resource diffs with action/type filters), module (tabular providers/inputs/outputs/tags), diagram (in-terminal or browser). Mouse and resize support |

### Execution Pipeline

```
1. Pre-parse --root-dir
2. Clean .terragrunt-cache and .terraform.lock.hcl
3. Parse CLI args (with .terrahawk.yml defaults)
4. Check dependencies
5. Setup output dirs and paths
6. detect_config_dir → find terragrunt root
7. Discover units (rglob terragrunt.hcl)
7b. Apply --unit filter (if --unit)
8. Apply incremental filter (if --incremental)
9. Query state ages (Azure Blob / AWS S3 / GCS)
10. Extract root provider template
11. Build DAG waves (if --dag)
12. Execute plans (ThreadPoolExecutor, wave-by-wave or all-at-once)
13. Process results (plan parsing, diagrams, tags, etc.)
14. Merge incremental results from previous report
15. Write JSON + manifest + HTML report
16. Cleanup tmp dir, restore .terraform.lock.hcl files
```

### HTML Report Template

The report at `src/terrahawk/templates/report.html` has all CSS and JavaScript inline. External dependencies: the Mermaid CDN for diagram rendering and a companion `_data.js` file for the report data.

The `_data.js` file is written by `report.py` and contains `window.TERRAHAWK_DATA=[...];`. The HTML loads it via `<script src="%%DATA_FILE%%">`, which works from `file://`, S3, Azure Blob, GCS, or any static hosting (no CORS issues, no server required).

Placeholders injected via substitution:
- `%%DATA_FILE%%` — filename of the companion `_data.js` file
- `%%REPORT_DATE%%` — generation timestamp
- `%%HAS_DIAGRAMS%%` / `%%HAS_TAGS%%` — feature flags
- `%%VERSIONS%%` — tool versions JSON

When modifying the template, maintain:
- Dark/light theme support (CSS custom properties)
- Mobile responsiveness
- Self-contained nature (no external CSS/JS besides Mermaid)

### State Age System

The `state_age.py` module queries three cloud backends. Key implementation details:

**Exact keys from render**: `_compute_state_age()` in `process.py` first tries the exact state key from the unit's rendered config (`render_json.remote_state.config.key`/`prefix`, captured by the worker via `terragrunt render --format json`), falling back to the `rel_path + "/terraform.tfstate"` heuristic. The regex extraction below still powers the one-time blob *listing*.

**Config scoping**: `_extract_remote_state_config()` parses the `remote_state { config = { ... } }` block specifically, so regex only matches backend config values — not provider or generate block values (a common source of bugs).

**Local resolution**: `_resolve_local()` resolves `${local.X}` interpolations by searching the root HCL and sibling `.hcl` files (e.g., `global.hcl`, `customer.hcl`).

**GCS** has the most robust implementation — resolves templates via `env.hcl` discovery, handles object versioning, normalizes timezone offsets. Use it as the reference when adding new backends.

**Azure** uses `az storage blob list` with JMESPath filtering for `.tfstate` files.

**AWS** uses `aws s3api list-objects-v2` and strips a service prefix from S3 keys when the key template includes `${local.Service}/`.

**Error handling**: All backends check CLI availability via `shutil.which()` and print diagnostic warnings on failure. Never silently swallow exceptions.

### Dockerfile

Multi-stage build producing **per-cloud images** (`--build-arg CLOUD=aws|azure|gcp`). Each variant ships only the CLI needed for its remote state backend:

- `binaries-common` — mise, terraform, terragrunt (shared)
- `binaries-aws` — AWS CLI v2 (installed with final paths to avoid library path issues)
- `binaries-azure` — (no extra binaries; azure-cli is pip-installed)
- `binaries-gcp` — google-cloud-sdk
- `pip-builder` — azure-cli (azure variant only)
- `runtime-base` — python:3.11-slim + git + openssh-client
- `final-{cloud}` — copies binaries + pip packages + terrahawk source

**Important**: The AWS CLI v2 installer bakes absolute paths into its launcher. Install with `--install-dir /usr/local/awscli --bin-dir /usr/local/bin` so paths match in the final image.

## Key Patterns

**Zero runtime dependencies**: The Python code uses only the standard library. No pip packages are required at runtime. This keeps the package simple and the Docker image small.

**Backward compatibility**: `terrahawk.py` at repo root is a thin shim that inserts `src/` into `sys.path` and delegates to `terrahawk.cli:main`. The Dockerfile entrypoint (`python3 /app/terrahawk.py`) is unchanged. `python -m terrahawk` also works via `__main__.py`.

**Config file parsing**: `.terrahawk.yml` is parsed with a simple line-by-line key-value parser — no PyYAML dependency. Only top-level scalar values are supported, plus the nested `unit_timeouts:` section.

**Thread-based parallelism**: Plans run in a `ThreadPoolExecutor` because they're I/O-bound (subprocess calls). The GIL is not a bottleneck.

**DAG execution**: Kahn's algorithm produces topological waves. Units within a wave run in parallel; waves run sequentially. Circular dependencies are caught and placed in a final wave.

**Incremental scanning**: Each unit's `terragrunt.hcl` is MD5-hashed. Only units whose hash changed since the last manifest are re-scanned. Previous results are merged from the last JSON report.

**Affected scanning** (`--affected [BASE]`): discovery runs `terragrunt find --filter=[BASE...HEAD]` (git-aware), scanning only units whose files — including local module sources and read files — changed since BASE. Unaffected units merge from the previous report like incremental mode. Requires Terragrunt 1.x + git; errors out otherwise (no silent fallback).

**HCL hygiene**: every scan runs `terragrunt hcl validate --json` + `terragrunt hcl format --check` once over the tree (`hygiene.py`). Findings map to units by path prefix → `hclIssues` / `unformattedFiles` entry fields → `hcl N` / `fmt` badges (HTML) and detail sections (TUI). Root-level findings print as console warnings. Silently skipped on pre-1.x terragrunt.

**Error classification**: Exit code 0 = clean, 2 = drift, 124/137 = timeout, anything else = error. Error output is extracted by matching Terraform error block patterns.

## Version Management

Version is defined in two places (keep synchronized):

1. `src/terrahawk/__init__.py` — `__version__ = "X.Y.Z"`
2. `pyproject.toml` — `version = "X.Y.Z"`

The `--version` flag reads from `__init__.py`.

## Testing Checklist

Before submitting changes, verify:

```bash
# Basic CLI
python3 terrahawk.py --version
python3 terrahawk.py --help
python3 -m terrahawk --version

# Module imports
python3 -c "import sys; sys.path.insert(0,'src'); from terrahawk import main; print('OK')"

# Docker (if Dockerfile changed)
docker build --build-arg CLOUD=aws -t terrahawk:aws .
docker run --rm terrahawk:aws --version
```

## Common Pitfalls

- **Regex matching wrong block**: Always scope regex searches to the relevant HCL block (e.g., `remote_state.config`), not the entire file. Provider blocks, generate blocks, and remote_state blocks often share key names like `region`, `profile`, `bucket`.
- **Silent failures in state_age**: Always print diagnostic warnings when CLI commands fail. Never use bare `except Exception: pass`.
- **AWS CLI path issues in Docker**: The AWS CLI v2 installer hardcodes paths. Install-time paths must match final image paths.
- **Cloud credential mounts**: `.aws`, `.azure`, `.config/gcloud` must be mounted **read-write** (not `:ro`) because some CLIs write token cache files.
- **Template changes**: The HTML template is a single file with inline CSS/JS. Test with both dark and light themes after changes.
