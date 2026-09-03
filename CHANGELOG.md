# Changelog

All notable changes to Terrahawk will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.0] - 2026-09-03

### Added

- **Multi-account scanning — one report across several AWS accounts.** New `--profile NAME` flag (repeatable; or `aws_profiles: a,b` in `.terrahawk.yml`). With 2+ profiles, terrahawk maps each unit to the profile owning its account — resolving each profile's account via `aws sts get-caller-identity` and each unit's account from its nearest `env.hcl` (`aws_account_id`, handling `get_env`/literal/`local.X`) — then injects the right `AWS_PROFILE` into every unit's plan/init/render (`profiles.py`, `worker.py`). A single profile applies to all units with no STS calls. The one-shot state-age listing runs under the state-bucket owner's profile (`root.hcl` `state_account_id`). Replaces the old workaround of running once per profile with `--exclude` and stitching separate reports.
- **Stack treeview in the HTML report.** Explicit-stack members now render as a collapsible, path-nested tree (`▤ <stack>` header with rollup status badges + unit count → `📁` folder rows for intermediate path segments → unit leaves under `├─`/`└─` guides) instead of flat blue-badge rows. Folders sort first (alphabetical), then leaf units. A section that is exactly one stack merges the group and stack headers into a single row. Leaf rows keep all per-unit affordances (status, badges, actions, expandable diff). Non-stack units still render flat.

### Changed

- **Discovery always skips hidden directories and module catalogs.** Any unit whose `rel_path` has a hidden segment (starts with `.`, e.g. `.migration-backup`, `.git`) or a `catalog` segment is dropped before `--exclude` (`_is_always_skipped()` in `discovery.py`). Terragrunt's own generated `.terragrunt-stack` / `.terragrunt-cache` dot-dirs are exempt (stack members are real units).
- **Stack display names drop the trailing region segment.** `stackName` now uses the stack root's full rel_path minus a trailing region (`dum/production/eu-west-1` → `dum/production`, `shared/eu-west-1` → `shared`), so distinct stacks no longer all collapse to `eu-west-1` (`_stack_display_name()` in `process.py`).

## [1.6.1] - 2026-08-27

### Changed

- **Plan parsing is now JSON-first.** The structured change list is built from the JSON plan (`terraform show -json <planfile>`), which is structurally exact, and the human-readable text diff is grafted on per resource as the display `body` (`_build_plan_resources()` in `process.py`). Previously the fragile `terraform plan` *text* parse was authoritative and JSON was only a fallback — wrapped lines, heredocs, or comments containing unbalanced braces could corrupt the brace-depth walk and drop or truncate resources. Text-only parsing is still used when no JSON plan was captured (older terraform). The plan summary line is likewise synthesized from the JSON change counts when terraform's own `Plan:` line is absent.
- **Bumped pinned Terraform `1.15.6 → 1.16.0`** (Dockerfile) — latest stable, picks up the patched Go stdlib/deps that most image CVEs live in. Terragrunt stays on the latest stable `1.1.3`.

### Fixed

- **Azure state-age no longer truncates at 5000 blobs.** `az storage blob list` caps at 5000 results by default and silently drops the rest; state-age queries now pass `--num-results "*"` to follow continuation tokens and list the whole container (`state_age.py`). (AWS `s3api list-objects-v2` and GCS `objects list` already auto-paginate, so no truncation there; the AWS query now also passes `--prefix` when the service prefix is known, to scope large shared-state buckets.)
- **Azure state-age uses AD token auth by default.** The blob listing now passes `--auth-mode login` (`_azure_auth_args()`), so a CI `az login` service principal with blob-data RBAC works without needing storage-account-key access — the recommended locked-down path. When an explicit credential (`AZURE_STORAGE_KEY`, `AZURE_STORAGE_ACCOUNT_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, or `AZURE_STORAGE_SAS_TOKEN`) is present in the environment, az's default key/SAS auth is used instead, so existing key-based setups are unchanged.
- **Hardened the plan-text brace walk against comments and heredocs** (`plan_parser.py`). `_count_braces` now stops at `#` / `//` line comments (an unbalanced brace in a comment no longer shifts depth), and `parse_plan_resources` tracks heredoc state (`<<-EOT … EOT`) so free-text bodies containing `{`/`}` can't truncate or overrun a resource block. The display `body` that gets grafted onto the JSON-authoritative change list is now reliable even for `user_data`/policy heredocs.
- **Corrected a misleading init-retry comment** (`worker.py`) that claimed `-upgrade` is never passed while the retry did pass it. `init -upgrade` is local-only (provider plugins + `.terraform.lock.hcl`, which cleanup restores from git) and never touches remote state, so the scan stays read-only; the comment now explains this.
- **Removed dead `prefix`-threading code** in `_extract_state_resources` (`process.py`) — state resource addresses are already fully qualified, so the unreachable prefix branch and its recursion argument were removed.
- **GCS root `locals` block is now brace-walked, not regex-truncated.** `_query_gcs_blob_dates` matched the block with `locals\s*\{(.*?)\n\}`, which stops at the first `\n}` — a nested map value (e.g. `default_tags = { ... }`) truncated the body and dropped every local after it, breaking bucket/prefix resolution. It now reuses the same depth-aware brace walk as the `remote_state` parser (factored into `_brace_block()`).
- **S3 state-age now maps multi-segment key prefixes.** Key-to-rel_path mapping previously only stripped a single leading `${local.X}/` segment (`re.match(r'\$\{local\.(\w+)\}/', ...)`), so templates with a literal prefix or multiple locals (e.g. `env/${local.Service}/${path_relative_to_include()}/…` or `${local.Env}/${local.Service}/…`) silently dropped state-age data for every unit. `_resolve_s3_static_prefix()` now resolves the entire static portion before `${path_relative_to_include()}` — literal segments plus any number of `${local.X}` refs — and bails (rather than mis-mapping) when an interpolation can't be resolved.

### Tests

- **`state_age.py` now has unit coverage** (`tests/test_state_age.py`, 27 cases) — timestamp offset normalisation, the brace-block walker (including nested and unbalanced input), `remote_state`/`config` scoping vs provider blocks, S3 static-prefix resolution (literal + multi-local), local resolution across sibling HCL files, provider-template heredoc extraction, and the CLI-missing guard paths. This was previously the largest untested, most parse-fragile module.
- **`worker.py` now has unit coverage** (`tests/test_worker.py`, 10 cases) — the `.terragrunt-cache` directory scorer (generated-vs-module precedence, `examples/`/`test/` exclusion), per-unit timeout resolution, and read-only plan-arg assembly including the `--no-hooks` experiment flags. The timeout and plan-arg logic were extracted from `run_plan` into `_resolve_timeout()` / `_build_plan_args()` to make them testable without spawning terragrunt.
- **`plan_parser.py` brace-walk coverage** (`tests/test_plan_parser.py`, +6 cases) — `_count_braces` string/comment handling and the heredoc-aware block walk.
- **`discovery.py` filesystem-helper coverage** (`tests/test_discovery.py`, +6 cases) — source-stack detection vs generated artifacts (`find_stack_files`), rglob unit discovery with root/cache/stack skipping (`_discover_rglob`), and the `dependency` config-path regex fallback (`_parse_deps_regex`).
- **`report.py` template + sidecar coverage** (`tests/test_report.py`, +4 cases) — placeholder presence in the HTML template and Mermaid sidecar delivery (writes `mermaid.min.js`, dedupes an existing one).
- **Azure auth-mode coverage** (`tests/test_state_age.py`) — `_azure_auth_args` login-by-default and credential-env fallthrough. Suite is 94 tests.

## [1.6.0] - 2026-08-25

### Added

- **Error taxonomy (`errorClass`)** — every failed unit is now classified from its error text into one of `config`, `auth`, `init`, `dependency`, `plan`, `timeout`, or `other` (via `_classify_error()` in `process.py`), stored on each result as `errorClass`. The HTML report renders a compact chip row breaking errors down by class so failures can be triaged at a glance instead of being lumped into a single "error" bucket.
- **`--fail-on never|drift|error`** — exit-code gating for CI. `error` exits non-zero (code `2`) on any error/timeout; `drift` exits `2` on any drift, error, or timeout; `never` (default) always exits `0`. Configurable via `fail_on` in `.terrahawk.yml`. Exit code `2` is reserved for "findings" so it is distinguishable from `1` (terrahawk internal failure). Both entrypoints (`terrahawk.py`, `python -m terrahawk`) now propagate `main()`'s return value.
- **`--diagram-assets inline|sidecar`** — controls Mermaid delivery. `inline` (default) embeds the runtime in the HTML for a single self-contained file; `sidecar` writes `mermaid.min.js` once next to the report and references it relatively (much smaller HTML, still fully offline/air-gapped, deduped across reports in the same directory). Configurable via `diagram_assets` in `.terrahawk.yml`.
- **Test suite & CI** — first `pytest` suite (`tests/`, 30 tests covering plan parsing, secret redaction, error classification, DAG wave ordering, and report generation) plus a GitHub Actions workflow (`.github/workflows/ci.yml`) running the tests on Python 3.9 & 3.13 and building the AWS Docker image (with a non-blocking Docker Scout scan).

### Changed

- **`--diagrams` and `--dag` now default to on.** Architecture diagrams are embedded and units execute in dependency-ordered topological waves out of the box. Opt out per-run with `--no-diagrams` / `--no-dag` (both accept the `--no-` prefix via `argparse.BooleanOptionalAction`), or persistently with `diagrams: false` / `dag: false` in `.terrahawk.yml`. An explicit `diagrams: true` / `dag: true` in config keeps working.
- **Self-contained, air-gapped reports.** The Mermaid runtime is now vendored (`src/terrahawk/templates/vendor/mermaid.min.js`, v11.17.1) and inlined directly into the generated HTML instead of being loaded from `cdn.jsdelivr.net`. Reports render diagrams with no network access. The vendored asset ships in both the pip package (`package-data`) and the Docker images (`COPY src/terrahawk/`); generation falls back to the CDN only if the vendored file is missing.
- **DAG flat-parallel fallback** — when the dependency graph has no cross-unit edges, wave scheduling is skipped and units run in flat parallelism, avoiding needless serialization now that `--dag` is on by default. Behavior is unchanged when real dependency ordering exists.
- **The repository-root `terragrunt.hcl` is no longer scanned as a `.` unit.** It has no `region.hcl`/`env.hcl` above it and always errored; it is now filtered out at discovery so it stops polluting the report and error counts.

### Security

- **Key-name secret redaction backstop** — plan diffs now redact string values whose attribute name implies a secret (`password`, `secret`, `token`, `private_key`, `access_key`, `client_secret`, `api_key`, `credential`, `passphrase`, …) even when the provider did not flag them `sensitive`. This runs after the existing provider-sensitive masking as a defensive layer for state/outputs lacking sensitive markers.

### Fixed

- **Docker credential permissions** — documented and corrected the run examples so the container (which runs as non-root uid `65532`) can read host credential files (typically mode `600`): run with `--user "$(id -u):$(id -g)" -e HOME=/tmp` and mount credentials under `/tmp`. Previously the documented mounts failed with `The config profile (...) could not be found`.

## [1.5.0] - 2026-08-19

### Added

- **Terragrunt stack drift scanning** — explicit stacks (`terragrunt.stack.hcl`) are now first-class. Before discovery, Terrahawk runs `terragrunt stack generate` in every stack root (`generate_stacks()` in `discovery.py`) so the materialised `.terragrunt-stack/` units are discovered, planned, and drift-reported like any other unit. Auto-detected (no stack files = no-op); disable with `--no-stacks` (or `no_stacks: true` in `.terrahawk.yml`). Generated trees are cleaned up after the scan, but only when this run generated them. Requires Terragrunt 1.x.
- **Stack unit markers** — stack-generated units carry `isStack`, `stackName`, and a `displayUnit` (with the `.terragrunt-stack/` segment collapsed out). The HTML report shows a blue `▤ stack · <name>` badge and a clean path; the TUI shows an `S` indicator in the list plus a `Stack:` line in the detail view. Environment/subscription grouping is derived from the clean path, so the generated marker no longer pollutes it.
- **Units-in-stack diagrams** — one Mermaid graph per stack (member units as status-coloured nodes, intra-stack `dependency → dependent` edges from the discovery DAG), built by `build_stack_graphs()` and written to `_data.js` as `window.TERRAHAWK_STACKS`. The report renders a "▤ Stacks" chip bar above the unit list; clicking a chip opens the graph in the shared pan/zoom Mermaid modal.

### Changed

- **Bumped pinned Terragrunt `1.1.1 → 1.1.3`**. Brings a provider-cache download hardening (secret URL segment prevents local credential leakage), `--filter` negation fixes (used by `--affected`), the `iam_role` self-assumption regression fix, per-unit `feature` flag defaults, and a 7–10× faster `find_in_parent_folders()`.

## [1.4.0] - 2026-07-21

### Added

- **`--no-hooks` flag** — skip `before_hook`/`after_hook`/`error_hook` blocks during `terragrunt plan` for a pure read-only drift scan. Appends Terragrunt's experimental `--experiment=optional-hooks --no-hooks` to the per-unit plan. Opt-in (default off); also configurable via `.terrahawk.yml`. Requires Terragrunt ≥1.0.8.

### Changed

- **Bumped pinned Terragrunt `1.0.8 → 1.1.1`** (now GA, backwards-compat guaranteed since 1.0). Brings the Content Addressable Store (deduplicates module source downloads across the many units scanned in parallel), generated-stack detection in `find`/git-filters, an S3 chained-role-assumption fix, `-lockfile=readonly` support, and patched go-git CVEs.
- **`--affected` accuracy** — with Terragrunt ≥1.0.7, edits to a local module source (and `tfr://` registry sources) are tracked as read files, so a module change correctly marks its consuming units as affected. Help text updated to note this.

### Fixed

- **rglob discovery fallback** now skips generated `.terragrunt-stack` directories (as it already does for `.terragrunt-cache`), preventing stale materialised stack units from being scanned on the pre-1.x fallback path.

## [1.3.1] - 2026-06-23

### Security

- **Docker image CVE reduction** — addressed Docker Scout findings (all critical/high CVEs live inside the bundled precompiled Go binaries, not the Debian base):
  - Bumped pinned tools to builds with patched Go stdlib/deps: Terraform `1.15.2 → 1.15.6`, AWS CLI `2.34.36 → 2.35.11`, gcloud `565.0.0 → 573.0.0`. (Terragrunt stays on the latest stable `1.0.8`; its bundled go-git CVEs are only fixed in the RC-only `1.1.0` line, adopted once stable.)
  - **Stripped unused gcloud surfaces** (gsutil, bq, app-engine, docker/git credential helpers) from the GCP image — terrahawk only runs `gcloud storage objects list`, so those bundled vulnerable binaries were dead weight; removing them cuts CVEs and size.
  - Added `scripts/build-push.sh` — manual release pipeline that builds, runs a `docker scout` gate, and pushes only on pass (configurable `GATE`, `DRY_RUN`, `ALLOW_KNOWN`).
  - Documented in the Dockerfile why Alpine is **not** adopted (glibc-built tool binaries can't run on musl; Scout's 0-CVE Alpine score is base-layer-only) and why `perl` can't be purged (git hard-depends on it).

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
