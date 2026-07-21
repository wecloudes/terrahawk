<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="src/terrahawk/templates/eagle-white.svg" height="100">
    <source media="(prefers-color-scheme: light)" srcset="src/terrahawk/templates/eagle.svg" height="100">
    <img src="src/terrahawk/templates/eagle.svg" alt="" height="100">
  </picture>
</p>
<h1 align="center">Terrahawk</h1>
<p align="center"><strong>Bird's eye view of your infra</strong></p>

Terrahawk is a Terragrunt infrastructure scanning tool that runs `terragrunt plan` across all units in parallel and generates a fully static, self-contained HTML report combining drift detection, architecture diagrams, module introspection, resource tagging, and state age tracking.

Designed to run on a daily schedule via CI/CD (GitHub Actions, GitLab CI, Azure DevOps, Jenkins, etc.). The generated report can be published to any static-compatible storage (S3, Azure Blob, GCS, MinIO) and viewed directly in the browser — no backend required.

---

## Features

- **Drift Detection** — Runs `terragrunt plan -detailed-exitcode` on every unit. Diffs are rendered inline with color-coded syntax highlighting (additions, deletions, modifications). Units are classified as clean, drift, error, or timeout.
- **Architecture Diagrams** (`--diagrams`) — Interactive [Mermaid](https://mermaid.js.org/) diagrams built from the Terraform plan and state, showing all resources with dependency arrows. Changed resources are color-coded by action. No external tools required.
- **Tag Extraction** (`--tags`) — Resource tags extracted from Terraform state via `terraform show -json`. Filter units by any tag key/value combination.
- **Module Info** — Module source, required providers with version constraints, input variables with type definitions and defaults, and output values from state.
- **State Age** — Queries the remote state backend (Azure Blob, AWS S3, GCS) for last-modified dates. Informational blue badges showing days since last apply.
- **Incremental Mode** (`--incremental`) — Only re-scans units whose `terragrunt.hcl` changed since the last report. Unchanged units are merged from the previous run.
- **Dependency-Aware Execution** (`--dag`) — Parses `dependency` blocks to build a DAG and executes units in topological waves, ensuring correct plan order.
- **Dark/Light Theme** — Toggle in the report header, persisted via localStorage.
- **CSV Export** — Export filtered results directly from the report.
- **Terminal Viewer** (`terrahawk view`) — Interactive curses-based TUI to browse scan results directly in the terminal:
  - Units grouped by environment/subscription with status badges and coverage bar
  - Filter by status (`f`), subscription (`s`), tag key/value with autocomplete (`t`), or free-text search (`/`)
  - Sort by status, name, or resource count (`o`)
  - Detail view with color-coded plan diffs, providers, inputs, outputs, and tags
  - Plan view with per-resource expand/collapse, action and type filtering
  - Module info view with aligned tables for providers, inputs, outputs, and tags
  - Architecture diagrams rendered in-terminal (`d`) or in browser with zoom/pan (`D`)
  - Text wrapping (`w`) and horizontal scroll (`←→`) for long lines
  - Mouse support (click to select, scroll wheel to navigate)

---

## Requirements

| Tool | Required | Purpose |
|------|----------|---------|
| [Terraform](https://github.com/hashicorp/terraform) / [OpenTofu](https://opentofu.org/) | Yes | Infrastructure as Code engine |
| [Terragrunt](https://github.com/gruntwork-io/terragrunt) | Yes | Infrastructure orchestration |
| [mise](https://mise.jdx.dev) | No | Version pinning for Terraform/Terragrunt (required when `terraform_version` or `terragrunt_version` is set) |
| [AWS CLI](https://github.com/aws/aws-cli) | No | State age queries (S3 backend) |
| [Azure CLI](https://github.com/Azure/azure-cli) | No | State age queries (Azure Blob backend) |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | No | State age queries (GCS backend) |

Python 3.9 or later.

---

## Quick Start

### Run directly

```bash
# From the root of your Terragrunt repository:
python3 terrahawk.py

# With all optional features:
python3 terrahawk.py --diagrams --tags --dag

# Scan a single unit:
python3 terrahawk.py --unit production/westeurope/app-gateway

# Scan a different directory:
python3 terrahawk.py --root-dir /path/to/repo

# View the latest report in the terminal:
python3 terrahawk.py view

# View a specific report:
python3 terrahawk.py view terrahawk_20260511_060000

# As a Python module:
python3 -m terrahawk --help
```

### Install as a package

```bash
pip install .
terrahawk --help
```

### Run with Docker

Cloud-specific images — each variant ships only the CLI needed for its remote state backend.

Prebuilt multi-arch images (linux/amd64 + linux/arm64) are published to [Docker Hub](https://hub.docker.com/r/wecloudes/terrahawk):

```bash
# Pull (pick your cloud backend):
docker pull wecloudes/terrahawk:aws     # moving tag — latest release
docker pull wecloudes/terrahawk:azure
docker pull wecloudes/terrahawk:gcp

# Or pin a release:
docker pull wecloudes/terrahawk:aws-1.3.1
```

Or build locally:

```bash
docker build --build-arg CLOUD=aws   -t terrahawk:aws   .
docker build --build-arg CLOUD=azure -t terrahawk:azure .
docker build --build-arg CLOUD=gcp   -t terrahawk:gcp   .
```

```bash
# Run (AWS S3 backend):
docker run --rm \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
  -v "$HOME/.aws":/home/nonroot/.aws \
  -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
  terrahawk:aws --root-dir /workspace --diagrams --tags
```

```bash
# Run (Azure Blob backend):
docker run --rm \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
  -v "$HOME/.azure":/home/nonroot/.azure \
  -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
  terrahawk:azure --root-dir /workspace
```

```bash
# Run (GCS backend):
docker run --rm \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
  -v "$HOME/.config/gcloud":/home/nonroot/.config/gcloud \
  -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
  terrahawk:gcp --root-dir /workspace
```

---

## CLI Options

```
usage: terrahawk [-h] [-r ROOT_DIR] [-u UNIT] [-p PARALLELISM] [-t TIMEOUT]
                 [--diagrams] [--tags] [--incremental] [--dag]
                 [--exclude EXCLUDE] [--no-hooks] [--terraform-version VERSION]
                 [--terragrunt-version VERSION] [--version]
```

| Flag | Default | Description |
|------|---------|-------------|
| `-r, --root-dir` | Current directory | Path to the repository root containing the Terragrunt structure |
| `-u, --unit` | `None` | Scan only the unit whose relative path matches this value |
| `-p, --parallelism` | `6` | Maximum concurrent `terragrunt plan` executions |
| `-t, --timeout` | `300` | Timeout in seconds per unit |
| `--diagrams` | `false` | Enable architecture diagrams from plan/state |
| `--tags` | `false` | Extract resource tags from Terraform state |
| `--incremental` | `false` | Only re-scan changed units since the last report |
| `--dag` | `false` | Execute units in dependency order (topological waves) |
| `--exclude` | `""` | Regex pattern to exclude unit paths |
| `--no-hooks` | `false` | Skip `before_hook`/`after_hook`/`error_hook` for a pure read-only drift scan (Terragrunt experimental `optional-hooks`, requires Terragrunt ≥1.0.8) |
| `--terraform-version` | System default | Pin Terraform to a specific version via [mise](https://mise.jdx.dev) |
| `--terragrunt-version` | System default | Pin Terragrunt to a specific version via [mise](https://mise.jdx.dev) |
| `--push-url` | `None` | Publish the report to a [Terrakettle](../terrakettle) server after the scan |
| `--push-token` | `$TERRAKETTLE_TOKEN` | Per-project Terrakettle push token |
| `--version` | | Print version and exit |

### Subcommands

| Command | Description |
|---------|-------------|
| `terrahawk view [report]` | Open the TUI to browse results in the terminal. Loads the latest report by default, or specify a report name/path. |

---

## Terminal Viewer

`terrahawk view` opens an interactive TUI to browse scan results without leaving the terminal.

```bash
terrahawk view                              # latest report
terrahawk view terrahawk_20260512_060000     # specific report by name
terrahawk view /path/to/report.json         # specific report by path
```

### Views

| Key | View | Description |
|-----|------|-------------|
| `Enter` | Detail | Full unit info: status, providers, plan diffs, errors, outputs, tags, inputs |
| `p` | Plan | Per-resource change list, expandable diffs, filter by action or resource type |
| `m` | Module | Tabular view of providers, input variables, outputs, and tags |
| `d` | Diagram | Architecture diagram rendered in the terminal |
| `D` | Diagram (browser) | Full interactive Mermaid diagram in the browser with zoom/pan |

### Keyboard Shortcuts

**List view:**

| Key | Action |
|-----|--------|
| `j/k` or `↑/↓` | Navigate units |
| `Enter` | Open detail view |
| `p` | Open plan view |
| `m` | Open module info |
| `d` | Open diagram (terminal) |
| `D` | Open diagram (browser) |
| `f` | Cycle status filter (all → drift → error → timeout → clean) |
| `s` | Cycle subscription filter |
| `t` | Tag filter with autocomplete (key or key=value) |
| `o` | Cycle sort mode (status → name → resources) |
| `/` | Free-text search |
| `c` | Clear all filters |
| `g/G` | Jump to top/bottom |
| `q` | Quit |

**Detail / Module / Diagram views:**

| Key | Action |
|-----|--------|
| `j/k` or `↑/↓` | Scroll vertically |
| `l` or `←/→` | Scroll horizontally |
| `w` | Toggle text wrapping |
| `0` | Reset horizontal scroll |
| `p/m/d/D` | Switch to plan/module/diagram view |
| `Esc/q` | Back to list |

**Plan view:**

| Key | Action |
|-----|--------|
| `Enter` | Expand/collapse resource diff |
| `f` | Cycle action filter (all → create → replace → update → delete → read) |
| `t` | Cycle resource type filter |
| `w` | Toggle text wrapping |
| `←/→` | Horizontal scroll |
| `Esc/q` | Back to list |

---

## Configuration File

Terrahawk supports a `.terrahawk.yml` file in the repository root. Values set here become defaults for all CLI options and can be overridden by command-line arguments.

```yaml
# .terrahawk.yml

parallelism: 10
timeout: 300
diagrams: true
tags: true
incremental: false
dag: false
exclude: "bootstrapping|test"

# Publish the report to a Terrakettle server after each scan (optional).
# The token is per-project; prefer the $TERRAKETTLE_TOKEN env var in CI.
push_url: "https://terrakettle.example.com"
push_token: ""

# Pin Terraform/Terragrunt versions (requires mise).
# When set, all commands are run via `mise exec <tool>@<version> --`.
# Omit or leave empty to use whatever is on PATH.
terraform_version: "1.9.0"
terragrunt_version: "0.68.0"

# Per-unit timeout overrides (in seconds).
# Keys are substring patterns matched against unit paths.
# First match wins.
unit_timeouts:
  ptn-alz: 600
  virtualwan: 600
  ptn-applicationgateway: 500
```

---

## Output

Each run produces three files in `terrahawk_results/`:

| File | Description |
|------|-------------|
| `terrahawk_YYYYMMDD_HHMMSS.html` | HTML report (open in any browser, or upload to static storage) |
| `terrahawk_YYYYMMDD_HHMMSS_data.js` | Report data loaded by the HTML via `<script src>` — must be kept alongside the HTML |
| `terrahawk_YYYYMMDD_HHMMSS.json` | Raw JSON data for programmatic consumption |

The HTML + `_data.js` pair work everywhere: `file://`, S3, Azure Blob, GCS, any static hosting. Just keep them in the same directory.

When using `--incremental`, a `.manifest` file is also written to track file hashes between runs.

### Publishing to Terrakettle

Pass `--push-url` (and a token via `--push-token` or `$TERRAKETTLE_TOKEN`) to publish the report to a [Terrakettle](../terrakettle) server, which stores run history per project and serves the interactive HTML report over the web:

```bash
terrahawk --push-url https://terrakettle.example.com   # token from $TERRAKETTLE_TOKEN
```

The push runs after the report is written and uses only the standard library. A push failure prints a warning but does not fail the scan.

---

## CI/CD Integration

### GitHub Actions

```yaml
name: Terrahawk Scan
on:
  schedule:
    - cron: '0 6 * * *'  # Daily at 06:00 UTC
  workflow_dispatch:

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Terrahawk
        run: |
          docker run --rm \
            -v "${{ github.workspace }}":/workspace \
            terrahawk:aws --root-dir /workspace --tags --dag

      - name: Upload Report
        uses: actions/upload-artifact@v4
        with:
          name: terrahawk-report
          path: terrahawk_results/
```

### Azure DevOps

```yaml
trigger: none
schedules:
  - cron: '0 6 * * *'
    displayName: Daily scan
    branches:
      include: [main]

pool:
  vmImage: ubuntu-latest

steps:
  - checkout: self

  - script: |
      docker run --rm \
        -v "$(Build.SourcesDirectory)":/workspace \
        terrahawk:azure --root-dir /workspace --tags
    displayName: Run Terrahawk

  - publish: terrahawk_results/
    artifact: terrahawk-report
```

---

## Project Structure

```
terrahawk/
├── terrahawk.py                 # Thin shim (entrypoint, backward compat)
├── pyproject.toml               # Package metadata
├── Dockerfile                   # Multi-cloud Docker images
├── src/terrahawk/
│   ├── __init__.py              # __version__, re-export main
│   ├── __main__.py              # python -m terrahawk
│   ├── cli.py                   # Main pipeline orchestration
│   ├── config.py                # Config file loading, config dir detection
│   ├── deps.py                  # Dependency checking, version detection
│   ├── discovery.py             # Unit discovery, DAG builder
│   ├── incremental.py           # Manifest hashing for incremental mode
│   ├── worker.py                # Plan execution worker
│   ├── plan_parser.py           # Terraform plan output parser
│   ├── process.py               # Result processing and assembly
│   ├── state_age.py             # Remote state age queries (Azure/AWS/GCS)
│   ├── report.py                # HTML report generation
│   ├── push.py                  # Publish report to a Terrakettle server
│   ├── tui.py                   # Terminal UI viewer (curses-based)
│   └── templates/
│       ├── report.html          # HTML report template
│       ├── eagle.svg            # Logo (light theme)
│       └── eagle-white.svg      # Logo (dark theme)
├── THIRD_PARTY_LICENSES         # Licenses for bundled/invoked tools
└── LICENSE                      # MIT
```

---

## License

[MIT](LICENSE)

Made with <3 by [WeCloud](https://www.wecloud.es/ "Cloud made simple").

If you find Terrahawk useful, please consider giving it a star on [GitHub](https://github.com/wecloudes/terrahawk) — it helps others discover the project!
