# Changelog

All notable changes to Terrahawk will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-06-23

### Added

- **Publish to Terrakettle** (`--push-url`, `--push-token`) — after a scan, the report triple (JSON + HTML + `_data.js`) is uploaded to a [Terrakettle](../terrakettle) server via `POST /api/v1/runs`, which stores per-project run history and serves the interactive report over the web. Token comes from `--push-token` or `$TERRAKETTLE_TOKEN`; both are also configurable via `.terrahawk.yml`. Stdlib-only (`urllib` + hand-rolled multipart, Bearer auth); a push failure prints a warning but never fails the scan.

## [1.2.0] - 2026-06-11

### Added

- **Native Terragrunt 1.x discovery** — units and their dependency graph are now discovered via `terragrunt find --format json --dependencies` (Terragrunt's own HCL parser), catching `dependencies { paths }` blocks, include-based dependencies, and stacks that the previous regex parsing missed. Falls back to the old glob+regex discovery on older Terragrunt versions.
- **Affected-only scanning** (`--affected [BASE]`) — scan only units affected by git changes since BASE (default `main`) using terragrunt's `--filter=[BASE...HEAD]`. Catches changes in local module sources and read files that the MD5-based `--incremental` mode misses. Unaffected units are merged from the previous report.
- **Out-of-band drift detection** — the plan JSON (`terraform show -json`) is now captured for every completed plan (exit 0 and 2) and its `resource_drift` array surfaces resources changed outside Terraform — even on units whose plan is otherwise clean. Shown as a `⚠ N ext` badge and expandable panel in the HTML report, and a "Changed Outside Terraform" section in the TUI.
- **Rendered config collection** — each unit runs `terragrunt render --format json` after its plan, providing: resolved module source, resolved input *values* (shown in a new "Value" column in module info; credential-like input names are masked), and the exact remote state key for state-age matching (replacing path heuristics).
- **HCL hygiene checks** — every scan runs `terragrunt hcl validate --json` and `terragrunt hcl format --check` once over the tree. Findings are attached per unit (`hcl N` / `fmt` badges in HTML, detail sections in the TUI); root-level findings are printed as console warnings.
- **`terrahawk graph`** — print the repo-level unit dependency graph as Mermaid, or write a self-contained HTML file with `-o graph.html` (powered by `terragrunt dag graph`).
- **`terrahawk list`** — instant unit inventory without scanning: dependency counts joined with last-report status, state age, resource count, and duration.
- **Per-unit scan duration** — recorded for every unit; shown in the console output (`DRIFT (12s)`), as a chip in the HTML report, as a column in the TUI list, and available as a "Slowest" sort in both UIs.
- **Structured plan fallback** — when text-based plan parsing yields nothing, resource changes are reconstructed from the plan JSON (with sensitive values masked).
- **Timeout visibility** — timeouts now get their own summary card, coverage-bar segment, and legend entry in the HTML report.
- **`terrahawk view -r DIR`** — the terminal viewer can now load reports from any repo root, not just the current directory.
- **Escape key** closes modals in the HTML report.

### Changed

- `terragrunt plan` now runs with `--non-interactive` and `--dependency-fetch-output-from-state` (dependency outputs read directly from remote state — significant speedup on deep dependency graphs), and the redundant `--no-auto-init=false` flag was removed.
- DAG waves are built from terragrunt-native dependency data when available (regex parsing remains as fallback).
- State age matching prefers the exact rendered remote-state key over the `rel_path + "/terraform.tfstate"` heuristic.

### Fixed

- Timeout badge used hardcoded dark-theme colors and was unreadable in light theme.

## [1.1.0] - 2026-05-12

### Added

- **Terminal viewer** (`terrahawk view [report]`) — full-featured curses-based TUI for browsing scan results in the terminal:
  - **List view** — units grouped by environment/subscription with colored status badges, coverage bar, resource counts, and state age
  - **Detail view** — color-coded plan diffs, providers, inputs, outputs, tags, and error output with vertical and horizontal scrolling
  - **Plan view** — per-resource change list with expand/collapse diffs, action filter (`f`), and resource type filter (`t`)
  - **Module info view** (`m`) — aligned tables for providers, input variables (name, type, default, description), outputs, and tags (with default/explicit source)
  - **Architecture diagrams** — in-terminal diagram view (`d`) and browser view (`D`) with zoom controls, fit-to-view, and click-and-drag panning
  - **Filtering** — status (`f`), subscription (`s`), tag key/value with Tab autocomplete and validation (`t`), free-text search (`/`)
  - **Sort toggle** (`o`) — cycle between status, name, and resource count
  - **Text wrapping** (`w`) — toggle word-boundary wrapping for long lines
  - **Mouse support** — click to select, scroll wheel to navigate
  - **Graceful error handling** — terminal restored cleanly on errors, "too small" message on tiny terminals, invalid tag filter feedback
- **Single-unit mode** (`-u, --unit`) — scan only a specific unit by its relative path (exact match or suffix match). Useful for debugging or quick-checking a single unit without running a full scan.
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
