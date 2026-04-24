# Changelog

All notable changes to Terrahawk will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Tool version pinning** — new `terraform_version` and `terragrunt_version` options (in `.terrahawk.yml` or via `--terraform-version` / `--terragrunt-version` CLI flags). When set, commands are executed via [mise](https://mise.jdx.dev) (`mise exec <tool>@<version> --`), allowing teams to lock specific versions without changing their system install. mise is bundled in the Docker images.
- **External report data** — the HTML report now loads its data from a companion `_data.js` file via `<script src>` instead of embedding all JSON inline. This keeps the HTML lightweight and works from `file://`, S3, Azure Blob, GCS, or any static hosting.

### Changed

- **State age badges** — switched from green/yellow/red severity colors to a uniform blue informational style, reflecting that state age is informational rather than an alert.

## [1.0.0] - 2026-04-17

### Added

- **Drift detection** — runs `terragrunt plan -detailed-exitcode` on every unit and classifies results as clean, drift, error, or timeout
- **Per-resource plan diffs** — structured parsing of `terraform plan` output with per-resource action breakdown (create, update, delete, replace, read)
- **Architecture diagrams** (`--diagrams`) — interactive Mermaid diagrams built from Terraform plan and state data, showing all resources with dependency arrows and color-coded actions. No external tools required
- **Tag extraction** (`--tags`) — resource tags from Terraform state via `terraform show -json`, with tag key/value filtering in the report
- **Module info** — module source, required providers with version constraints, input variables with type definitions and defaults, output values from state
- **State age tracking** — queries remote state backends (Azure Blob Storage, AWS S3, Google Cloud Storage) for last-modified dates with color-coded age badges
- **Incremental mode** (`--incremental`) — only re-scans units whose `terragrunt.hcl` changed since the last report, merging unchanged units from the previous run
- **Dependency-aware execution** (`--dag`) — parses `dependency` blocks to build a DAG and executes units in topological waves via Kahn's algorithm
- **Parallel execution** (`-p N`) — configurable parallelism for concurrent `terragrunt plan` runs
- **Per-unit timeout** (`-t N`) — configurable timeout with per-unit overrides via `.terrahawk.yml` `unit_timeouts` section
- **Exclude patterns** (`--exclude`) — regex-based unit path exclusion
- **Configuration file** — `.terrahawk.yml` for setting defaults across all CLI options
- **Auto-retry on provider errors** — automatic `terragrunt init -upgrade` retry when provider/plugin issues are detected
- **HTML report** — fully static, self-contained single-file report with:
  - Dark/light theme toggle (persisted via localStorage)
  - Status/subscription/tag filtering with free-text search and highlighting
  - Sort by status, name, or resource count
  - CSV export of filtered results
  - Inline color-coded diffs (additions, deletions, modifications)
  - Expandable per-resource plan details with action type filtering
  - Mermaid diagram modal with mouse-wheel zoom and click-and-drag panning
  - Coverage bar and summary statistics
  - About panel with tool versions and feature documentation
- **JSON report** — raw structured data for programmatic consumption
- **Docker support** — multi-stage Dockerfile with per-cloud variants (`aws`, `azure`, `gcp`), each shipping only the CLI needed for its remote state backend
- **Python package structure** — `src/terrahawk/` layout with `pyproject.toml`, `python -m terrahawk` support, and backward-compatible `terrahawk.py` shim
- **Config directory auto-detection** — walks common Terragrunt directory structures to find the config root
- **Provider template fallback** — extracts `generate "provider"` template from root HCL for units without a populated cache
- **Git cleanup** — automatic restoration of `.terraform.lock.hcl` files modified during the scan
