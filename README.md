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
- **Architecture Diagrams** (on by default, `--no-diagrams` to disable) — Interactive [Mermaid](https://mermaid.js.org/) diagrams built from the Terraform plan and state, showing all resources with dependency arrows. Changed resources are color-coded by action. The Mermaid runtime is vendored and inlined into the report, so diagrams render fully offline / air-gapped — no CDN, no external tools required.
- **Tag Extraction** (`--tags`) — Resource tags extracted from Terraform state via `terraform show -json`. Filter units by any tag key/value combination.
- **Module Info** — Module source, required providers with version constraints, input variables with type definitions and defaults, and output values from state.
- **State Age** — Queries the remote state backend (Azure Blob, AWS S3, GCS) for last-modified dates. Informational blue badges showing days since last apply.
- **Incremental Mode** (`--incremental`) — Only re-scans units whose `terragrunt.hcl` changed since the last report. Unchanged units are merged from the previous run.
- **Dependency-Aware Execution** (on by default, `--no-dag` to disable) — Parses `dependency` blocks to build a DAG and executes units in topological waves, ensuring correct plan order. When there are no cross-unit dependencies it falls back to flat parallelism automatically.
- **Encoding-Only Change Detection** — Planned changes whose only difference is a base64-gzip string that decodes to identical content (e.g. `user_data_base64 = base64gzip(...)` after Terragrunt 1.2 / OpenTofu 1.13 switched to the Go 1.27 gzip encoder) get a blue `gz` badge and a per-resource "gzip re-encode only" tag. The action still counts as drift — a replace-on-change will really replace the instance — but the report shows that the content didn't change. `base64gzip_compat()` keeps the old bytes.
- **Full Refresh Enforced** — `-refresh=false` and Terraform 1.17's `-minimal-refresh` hide out-of-band drift, so they are stripped from an inherited `TF_CLI_ARGS` / `TF_CLI_ARGS_plan` (with a warning). Flags injected via Terragrunt `extra_arguments` are not touched.
- **Error Taxonomy** — Failed units are classified (`config`, `auth`, `init`, `dependency`, `throttled`, `plan`, `timeout`) and the report shows a per-class breakdown, so failures are triageable at a glance instead of one undifferentiated "error" bucket.
- **CI Gating** (`--fail-on drift|error|new`) — Exit non-zero (code `2`) when the scan finds drift or errors — or, with `new`, only drift/errors that weren't there in the previous run — so a scheduled scan can fail a pipeline. Ignored drift never fails. Default `never` (always exit `0`).
- **Since Last Run** (`--baseline`) — Every scan is compared with the previous report: drift and errors are marked **NEW**, **persisting** (with how many days) or **resolved**, and individual resource changes not seen before are tagged `new`. The report shows a "Since last run" chip bar and a trend filter. Every full scan also writes fixed-name copies — `latest.json`, `latest.html`, `latest_data.js` — and the next run uses `terrahawk_results/latest.json` as its baseline when present (else the newest full report there). On a fresh CI runner, download the previously published `latest.json` into `terrahawk_results/` before the scan and publish the folder after it (see [CI/CD Integration](#cicd-integration)); `--baseline PATH|URL` overrides the choice. `--unit` reports never replace `latest.*` and are never used as a baseline.
- **Ignore Known Drift** (`.terrahawkignore`) — Accepted drift (tags owned by a tagging service, autoscaling counts, ...) is suppressed by unit / address / attribute rules with an optional `until=` expiry. Ignored changes stay visible but dimmed; a unit whose every change is ignored counts as clean. Expired and unused rules are reported. See [Ignoring known drift](#ignoring-known-drift).
- **Owners** — Units are mapped to owners from the repo's `CODEOWNERS` (GitHub rules; GitLab sections supported). Owners show on every unit, in alerts, SARIF and JUnit, and the report can be filtered by owner.
- **SARIF & JUnit** (`--sarif PATH`, `--junit PATH`) — CI-native findings: SARIF 2.1.0 for GitHub code scanning (one alert per drifted resource / failed unit, stable fingerprints across runs), JUnit XML for GitLab, Azure DevOps and Jenkins test views.
- **Slack / Teams Alerts** — A summary is posted when new drift or errors appear (`--notify-on new|findings|never`). The webhook URL is read **only** from the `TERRAHAWK_WEBHOOK_URL` environment variable (inject it from your CI secret store) — never from `.terrahawk.yml` — and is never printed. Teams uses Adaptive Cards (Teams Workflows webhooks).
- **Rate-Limit Retries** (`--throttle-retries N`, default `2`) — Units that fail on provider API throttling (`Throttling`, `429`, `TooManyRequests`, `rateLimitExceeded`, ...) are re-planned with jittered backoff within their timeout, instead of being reported as plan errors. Units still throttled after the retries are classified `throttled`.
- **Terragrunt Stacks** (auto, `--no-stacks` to disable) — Explicit stacks (`terragrunt.stack.hcl`) are auto-materialised via `terragrunt stack generate` before discovery, so their generated units are drift-scanned like any other unit. Stack units get a `▤ stack · <name>` badge, and each stack renders a **units-in-stack** Mermaid diagram (members coloured by status, intra-stack dependency edges) from a "▤ Stacks" chip bar. Generated trees are cleaned up afterwards. Requires Terragrunt 1.x.
- **Themes** — Dark, Light, Tokyo Night and Cyberpunk from the header menu, persisted via localStorage; `?theme=<name>` opens a report in a given theme.
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

# Diagrams and DAG execution are on by default; add tag extraction:
python3 terrahawk.py --tags

# Gate a CI run: exit non-zero if any drift or errors are found:
python3 terrahawk.py --fail-on drift

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
docker pull wecloudes/terrahawk:aws-1.9.1
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
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/tmp/.ssh:ro \
  -v "$HOME/.aws":/tmp/.aws \
  -v "$HOME/.gitconfig":/tmp/.gitconfig:ro \
  terrahawk:aws --root-dir /workspace --diagrams --tags
```

```bash
# Run (Azure Blob backend):
docker run --rm \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/tmp/.ssh:ro \
  -v "$HOME/.azure":/tmp/.azure \
  -v "$HOME/.gitconfig":/tmp/.gitconfig:ro \
  terrahawk:azure --root-dir /workspace
```

```bash
# Run (GCS backend):
docker run --rm \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/workspace \
  -v "$HOME/.ssh":/tmp/.ssh:ro \
  -v "$HOME/.config/gcloud":/tmp/.config/gcloud \
  -v "$HOME/.gitconfig":/tmp/.gitconfig:ro \
  terrahawk:gcp --root-dir /workspace
```

### Credentials & file permissions (Docker)

The image runs as a non-root user (uid:gid `65532:65532`). Your host credential
files (`~/.aws`, `~/.azure`, `~/.config/gcloud`) are typically mode `600` and
owned by *your* host user, so uid 65532 inside the container cannot read them —
terrahawk then fails with errors like `The config profile (...) could not be found`.

The fix (already shown in the examples above) is to run the container **as your
host user** so it can read the mounted files:

```bash
--user "$(id -u):$(id -g)" -e HOME=/tmp
```

Because that uid has no entry in the image's `/etc/passwd`, point `HOME` at the
world-writable `/tmp` and mount your credential/config files under `/tmp`
(`/tmp/.aws`, `/tmp/.azure`, `/tmp/.config/gcloud`, `/tmp/.ssh`, `/tmp/.gitconfig`).
This also makes the generated report files in `terrahawk_results/` owned by you
instead of uid 65532. Keep the credential mounts read-write (not `:ro`) — some
CLIs write token-cache files into them.

---

## CLI Options

```
usage: terrahawk [-h] [-r ROOT_DIR] [-u UNIT] [-p PARALLELISM] [-t TIMEOUT]
                 [--diagrams] [--tags] [--incremental] [--dag]
                 [--exclude EXCLUDE] [--no-hooks] [--no-provider-cache] [--no-stacks] [--terraform-version VERSION]
                 [--terragrunt-version VERSION] [--version]
```

| Flag | Default | Description |
|------|---------|-------------|
| `-r, --root-dir` | Current directory | Path to the repository root containing the Terragrunt structure |
| `-u, --unit` | `None` | Scan only the unit whose relative path matches this value |
| `-p, --parallelism` | `6` | Maximum concurrent `terragrunt plan` executions |
| `-t, --timeout` | `300` | Timeout in seconds per unit |
| `--diagrams` / `--no-diagrams` | `true` | Embed architecture diagrams from plan/state (on by default; Mermaid is inlined, no network needed) |
| `--tags` | `false` | Extract resource tags from Terraform state |
| `--incremental` | `false` | Only re-scan changed units since the last report |
| `--dag` / `--no-dag` | `true` | Execute units in dependency order (topological waves); on by default |
| `--exclude` | `""` | Regex pattern to exclude unit paths |
| `--no-hooks` | `false` | Skip `before_hook`/`after_hook`/`error_hook` for a pure read-only drift scan (requires Terragrunt ≥1.0.8; GA in 1.2 — the `optional-hooks` experiment flag is added automatically on older versions) |
| `--no-provider-cache` | `false` | Use Terraform's shared plugin cache dir instead of Terragrunt's provider cache server (default on Terragrunt 1.x). The plugin dir isn't safe for parallel `init` on a cold cache — only opt out if the cache server can't reach your registries |
| `--no-stacks` | `false` | Skip `terragrunt stack generate` for `terragrunt.stack.hcl` files. By default stacks are auto-generated before discovery so their units are drift-scanned (Terragrunt 1.x) |
| `--terraform-version` | System default | Pin Terraform to a specific version via [mise](https://mise.jdx.dev) |
| `--terragrunt-version` | System default | Pin Terragrunt to a specific version via [mise](https://mise.jdx.dev) |
| `--fail-on` | `never` | Exit with a nonzero status for CI gating: `never`, `drift` (any drift or error), `error` (only unit errors/timeouts), or `new` (only drift/errors new since the baseline) |
| `--baseline` | newest full report | Previous JSON report to compare against (local path or `http(s)://` URL) |
| `--ignore-file` | `.terrahawkignore` | Known-drift ignore rules, relative to the root dir |
| `--codeowners` | auto | CODEOWNERS file for unit owners (default: `.github/`, root, `docs/`, `.gitlab/`) |
| `--sarif` | `None` | Also write findings as SARIF 2.1.0 |
| `--junit` | `None` | Also write a JUnit XML report |
| `--notify-on` | `new` | When to post the Slack/Teams summary (`new`, `findings`, `never`); needs `TERRAHAWK_WEBHOOK_URL` |
| `--report-url` | `None` | Link to the published report in alerts (a URL ending in `/` gets the report file name appended) |
| `--throttle-retries` | `2` | Re-plans after provider API rate limiting (`0` disables) |
| `--diagram-assets` | `inline` | Where diagram data lives: `inline` embeds Mermaid in the HTML, `sidecar` writes it once beside the report |
| `--push-url` | `None` | Publish the report to a [Terrakettle](../terrakettle) server after the scan |
| `--push-token` | `$TERRAKETTLE_TOKEN` | Per-project Terrakettle push token |
| `--version` | | Print version and exit |

### Subcommands

| Command | Description |
|---------|-------------|
| `terrahawk view [report]` | Open the TUI to browse results in the terminal. Loads the latest report by default, or specify a report name/path. |
| `terrahawk graph [-o FILE.html]` | Print the unit dependency graph as Mermaid (or write HTML) |
| `terrahawk list` | Unit inventory with the last report's status (no scan) |

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
diagrams: true     # embed architecture diagrams (default: true; set false to disable)
tags: true
incremental: false
dag: true          # dependency-ordered execution (default: true; set false to disable)
fail_on: never     # never | drift | error | new — exit code 2 on findings, for CI gating
baseline: ""       # previous report to compare against (path or https URL; default: newest in terrahawk_results/)
ignore_file: .terrahawkignore
codeowners: ""     # default: auto-detect .github/CODEOWNERS, CODEOWNERS, docs/, .gitlab/
sarif: ""          # e.g. terrahawk.sarif
junit: ""          # e.g. terrahawk-junit.xml
notify_on: new     # new | findings | never  (webhook URL: TERRAHAWK_WEBHOOK_URL env var ONLY)
report_url: ""     # where the published report lives, linked from alerts
throttle_retries: 2
diagram_assets: inline   # inline | sidecar — embed Mermaid in the HTML, or write it once beside the report
exclude: "bootstrapping|test"
no_hooks: false    # skip before/after/error hooks (read-only drift scan)
no_stacks: false   # skip auto `terragrunt stack generate` for terragrunt.stack.hcl
no_provider_cache: false  # use TF_PLUGIN_CACHE_DIR instead of Terragrunt's provider cache

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
| `latest.json` / `latest.html` / `latest_data.js` | Copies of the newest **full** scan under fixed names (not written by `--unit` runs): a stable URL for dashboards and alerts, and the next run's baseline |

The HTML + `_data.js` pair work everywhere: `file://`, S3, Azure Blob, GCS, any static hosting. Just keep them in the same directory.

When using `--incremental`, a `.manifest` file is also written to track file hashes between runs.

Each JSON entry carries the run-over-run fields `trend` (`new` / `persisting` / `resolved` / empty), `prevStatus` and `since` (date the current finding was first seen), plus `owners`, `ignoredCount` / `ignoredStatus`, and per-resource `isNew` / `ignored` / `changedAttrs` (attribute names only — values are never written).

### Ignoring known drift

Put a `.terrahawkignore` in the repository root (or point `--ignore-file` elsewhere). One rule per line:

```
# <unit> <address> [<attr>[,<attr>...]] [until=YYYY-MM-DD] [# reason]
*                *                          tags,tags_all      # tags owned by the tagging service
prod/*           aws_autoscaling_group.*    desired_capacity   # autoscaling
shared/vpc       aws_security_group.legacy  until=2026-12-31   # removal ticket open
```

- `<unit>` matches the unit path, `<address>` the resource address. `*` matches anything (including `/` and `.`), `?` one character; everything else is literal — `aws_instance.web["a"]` needs no escaping.
- Without attributes the whole resource change is ignored. With attributes, an update/replace is ignored only when **every** changed attribute is covered (`tags` also covers `tags.Owner`). Creates and deletes are never ignored by attribute rules.
- `until=` makes a rule expire (inclusive) so accepted drift resurfaces; expired rules and rules that matched nothing are printed after a full scan.
- A drift unit whose every planned change is ignored is reported **clean** (the JSON keeps `ignoredStatus: "drift"`), so it no longer trips `--fail-on` or alerts.

### Alerts

```bash
export TERRAHAWK_WEBHOOK_URL=...   # from your CI secret store — never commit it
terrahawk --notify-on new --report-url https://reports.example.com/terrahawk/
```

The kind is detected from the URL (`hooks.slack.com` → Slack, otherwise Teams); override with `TERRAHAWK_WEBHOOK_KIND=slack|teams`. A failed post prints a warning without the URL and never fails the scan. A `webhook*` key in `.terrahawk.yml` is ignored with a warning — rotate that webhook.

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

      # Credentials via OIDC (no long-lived keys in the repo or secrets).
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.TERRAHAWK_ROLE_ARN }}
          aws-region: eu-west-1

      # Fresh runners have no history: fetch the last full report as the baseline.
      - name: Download baseline
        run: |
          mkdir -p terrahawk_results
          aws s3 cp s3://my-reports/terrahawk/latest.json terrahawk_results/latest.json || echo "no baseline yet"

      - name: Run Terrahawk
        env:
          TERRAHAWK_WEBHOOK_URL: ${{ secrets.TERRAHAWK_WEBHOOK_URL }}
        run: |
          docker run --rm \
            --user "$(id -u):$(id -g)" -e HOME=/tmp \
            -e TERRAHAWK_WEBHOOK_URL -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
            -e AWS_SESSION_TOKEN -e AWS_REGION \
            -v "${{ github.workspace }}":/workspace \
            terrahawk:aws --root-dir /workspace --tags \
              --report-url https://my-reports.example.com/terrahawk/latest.html \
              --sarif /workspace/terrahawk.sarif --junit /workspace/terrahawk-junit.xml

      # Publish: latest.* becomes tomorrow's baseline and a stable report URL.
      - name: Publish report
        if: always()
        run: aws s3 sync terrahawk_results/ s3://my-reports/terrahawk/ --exclude ".tmp_*"

      - name: Upload SARIF (code scanning annotations)
        if: always()
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: terrahawk.sarif
          category: terrahawk

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

  # Fresh agents have no history: fetch the last full report as the baseline.
  - task: AzureCLI@2
    displayName: Download baseline
    inputs:
      azureSubscription: terrahawk-service-connection
      scriptType: bash
      scriptLocation: inlineScript
      inlineScript: |
        mkdir -p terrahawk_results
        az storage blob download --auth-mode login --account-name myreports \
          --container-name terrahawk --name latest.json \
          --file terrahawk_results/latest.json || echo "no baseline yet"

  - script: |
      docker run --rm \
        -e TERRAHAWK_WEBHOOK_URL \
        -v "$(Build.SourcesDirectory)":/workspace \
        terrahawk:azure --root-dir /workspace --tags \
          --junit /workspace/terrahawk-junit.xml
    displayName: Run Terrahawk
    env:
      TERRAHAWK_WEBHOOK_URL: $(TERRAHAWK_WEBHOOK_URL)   # secret pipeline variable / Key Vault

  - task: PublishTestResults@2
    condition: always()
    inputs:
      testResultsFormat: JUnit
      testResultsFiles: terrahawk-junit.xml
      testRunTitle: Terrahawk drift

  # Publish: latest.* becomes tomorrow's baseline and a stable report URL.
  - task: AzureCLI@2
    displayName: Publish report
    condition: always()
    inputs:
      azureSubscription: terrahawk-service-connection
      scriptType: bash
      scriptLocation: inlineScript
      inlineScript: |
        az storage blob upload-batch --auth-mode login --account-name myreports \
          --destination terrahawk --source terrahawk_results --overwrite

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
│   ├── cli.py                   # Subcommand dispatch + scan orchestrator
│   ├── options.py               # Scan CLI options (.terrahawk.yml defaults)
│   ├── commands.py              # view / graph / list subcommands
│   ├── phases.py                # Scan pipeline phases
│   ├── findings.py              # Owners, ignore rules, trend; CI exit code
│   ├── config.py                # Config file loading, config dir detection
│   ├── deps.py                  # Dependency checking, version detection
│   ├── discovery.py             # Unit discovery, DAG builder
│   ├── incremental.py           # Manifest hashing for incremental mode
│   ├── worker.py                # Plan execution worker
│   ├── plan_parser.py           # Terraform plan output parser
│   ├── process.py               # Result processing and assembly
│   ├── state_age.py             # Remote state age queries (Azure/AWS/GCS)
│   ├── history.py               # Run-over-run comparison (new/persisting/resolved)
│   ├── ignore.py                # .terrahawkignore rules
│   ├── owners.py                # CODEOWNERS → unit owners
│   ├── export.py                # SARIF + JUnit outputs
│   ├── notify.py                # Slack / Teams webhook alerts
│   ├── report.py                # HTML report generation
│   ├── push.py                  # Publish report to a Terrakettle server
│   ├── tui/                     # Terminal UI viewer (curses; one module per view)
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
