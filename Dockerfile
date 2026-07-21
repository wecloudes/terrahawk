# syntax=docker/dockerfile:1.7
#
# Terrahawk — one image per cloud backend (aws / azure / gcp).
# Each variant only ships the CLI needed for its remote-state backend, so
# the resulting images are substantially smaller than a single combined build.
#
# Build (pick one):
#   docker build --build-arg CLOUD=aws   -t terrahawk:aws   .
#   docker build --build-arg CLOUD=azure -t terrahawk:azure .
#   docker build --build-arg CLOUD=gcp   -t terrahawk:gcp   .
#
# Run (AWS S3 backend):
#   docker run --rm \
#     -v "$PWD":/workspace \
#     -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
#     -v "$HOME/.aws":/home/nonroot/.aws \
#     -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
#     terrahawk:aws --root-dir /workspace
#
# Run (Azure Blob backend):
#   docker run --rm \
#     -v "$PWD":/workspace \
#     -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
#     -v "$HOME/.azure":/home/nonroot/.azure \
#     -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
#     terrahawk:azure --root-dir /workspace
#
# Run (GCP backend):
#   docker run --rm \
#     -v "$PWD":/workspace \
#     -v "$HOME/.ssh":/home/nonroot/.ssh:ro \
#     -v "$HOME/.config/gcloud":/home/nonroot/.config/gcloud \
#     -v "$HOME/.gitconfig":/home/nonroot/.gitconfig:ro \
#     terrahawk:gcp --root-dir /workspace

ARG CLOUD=aws
# Pinned tool versions — bump in lockstep and re-run a `docker scout` scan
# (scripts/build-push.sh) before publishing. Most CVEs in these images live
# inside these precompiled Go binaries (terraform/terragrunt/gcloud), so a
# version bump to a build with patched Go stdlib / deps is the primary lever.
# terragrunt tracks the latest *stable* (1.1.x line: backwards-compat
# guaranteed since 1.0, go-git CVEs patched). 1.1.x also brings CAS
# (source-download dedup across parallel units), generated-stack detection
# in find/git-filters, S3 chained-role fix, and lockfile-readonly support.
ARG TERRAFORM_VERSION=1.15.6
ARG TERRAGRUNT_VERSION=1.1.1
ARG AWSCLI_VERSION=2.35.11
ARG GCLOUD_VERSION=573.0.0

# ============================================================
# Stage: common binaries (mise, terraform, terragrunt)
# Used by every cloud variant.
# mise is installed so that users can pin terraform/terragrunt
# versions at runtime via .terrahawk.yml or CLI flags.
# The default terraform/terragrunt binaries are still baked in
# for zero-config usage.
# ============================================================
FROM debian:12-slim AS binaries-common
ARG TERRAFORM_VERSION
ARG TERRAGRUNT_VERSION
ARG TARGETARCH

# Set the timezone environment variable
ENV TZ="Europe/Madrid"

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip ca-certificates tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tools

RUN ARCH=$([ "${TARGETARCH:-amd64}" = "arm64" ] && echo arm64 || echo amd64) && \
    echo "ARCH=$ARCH" > /env

# Pre-create the Terraform plugin cache with nonroot (uid:gid = 65532) ownership
# so the runtime image doesn't warn about a missing TF_PLUGIN_CACHE_DIR.
RUN mkdir -p /cache/plugins && chown -R 65532:65532 /cache

# mise — runtime version manager (used when terraform_version / terragrunt_version are set)
RUN . /env && \
    curl -fsSL "https://mise.jdx.dev/install.sh" | MISE_INSTALL_PATH=/tools/mise sh

# Terraform
RUN . /env && \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" -o terraform.zip && \
    unzip -q terraform.zip && rm terraform.zip && chmod +x terraform

# Terragrunt
RUN . /env && \
    curl -fsSL "https://github.com/gruntwork-io/terragrunt/releases/download/v${TERRAGRUNT_VERSION}/terragrunt_linux_${ARCH}" -o terragrunt && \
    chmod +x terragrunt

# ============================================================
# Stage: AWS-specific binaries (aws cli v2, self-contained)
# ============================================================
FROM binaries-common AS binaries-aws
ARG AWSCLI_VERSION
ARG TARGETARCH

RUN AWS_ARCH=$([ "${TARGETARCH:-amd64}" = "arm64" ] && echo aarch64 || echo x86_64) && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWSCLI_VERSION}.zip" -o awscli.zip && \
    unzip -q awscli.zip && \
    ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/awscli && \
    rm -rf awscli.zip aws

# ============================================================
# Stage: Azure-specific binaries (none — azure-cli comes via pip)
# ============================================================
FROM binaries-common AS binaries-azure

# ============================================================
# Stage: GCP-specific binaries (google-cloud-sdk)
# ============================================================
FROM binaries-common AS binaries-gcp
ARG GCLOUD_VERSION
ARG TARGETARCH

RUN GCLOUD_ARCH=$([ "${TARGETARCH:-amd64}" = "arm64" ] && echo arm || echo x86_64) && \
    curl -fsSL "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${GCLOUD_ARCH}.tar.gz" -o gcloud.tgz && \
    mkdir -p /opt && tar -xzf gcloud.tgz -C /opt && rm gcloud.tgz && \
    # Drop components not used by terrahawk to shave size AND cut CVEs.
    # terrahawk only ever runs `gcloud storage objects list` (state_age.py),
    # so the gsutil/bq surfaces and their bundled vulnerable Go/Python
    # binaries (docker-credential-gcloud, dev_appserver, etc.) are dead weight.
    # Keep: gcloud, gcloud-crc32c (storage checksums), bootstrapping.
    rm -rf /opt/google-cloud-sdk/help \
    /opt/google-cloud-sdk/platform/bundledpythonunix \
    /opt/google-cloud-sdk/platform/ext-runtime \
    /opt/google-cloud-sdk/platform/gsutil \
    /opt/google-cloud-sdk/platform/bq \
    /opt/google-cloud-sdk/platform/google_appengine \
    /opt/google-cloud-sdk/.install/.backup \
    /opt/google-cloud-sdk/bin/gsutil \
    /opt/google-cloud-sdk/bin/bq \
    /opt/google-cloud-sdk/bin/docker-credential-gcloud \
    /opt/google-cloud-sdk/bin/git-credential-gcloud.sh \
    /opt/google-cloud-sdk/bin/dev_appserver.py \
    /opt/google-cloud-sdk/bin/java_dev_appserver.sh && \
    find /opt/google-cloud-sdk -type f -name '*.pyc' -delete && \
    find /opt/google-cloud-sdk -type d -name __pycache__ -prune -exec rm -rf {} +

# ============================================================
# Pick the cloud-specific binaries stage
# ============================================================
FROM binaries-${CLOUD} AS binaries

# ============================================================
# Stage: Python packages — azure-cli on azure variant
# Built into /install so we can copy it wholesale into the final image.
# Python version matches the distroless runtime (debian 12 ships Python 3.13),
# so /install/lib/python3.13/site-packages aligns with what the final stage
# looks up at runtime.
# ============================================================
FROM python:3.13-slim AS pip-builder
ARG CLOUD

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

RUN if [ "${CLOUD}" = "azure" ]; then \
    pip install --no-cache-dir --prefix=/install --no-compile azure-cli && \
    find /install -type d \( -name __pycache__ -o -name tests -o -name test \) -prune -exec rm -rf {} + && \
    find /install -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete ; \
    else mkdir -p /install/lib /install/bin ; \
    fi

# ============================================================
# Stage: shared runtime base for all final images
# terrahawk shells out to git (to restore .terraform.lock.hcl files)
# and to `find`/`rm` for cache cleanup, so the runtime needs a real
# shell, coreutils, findutils and git on top of the Python runtime.
# gcloud also ships a bash launcher that execs python, so python:3.13-slim
# is a natural common base (it matches the pip-builder's interpreter too).
# ============================================================
# Base image: python:3.13-slim (Debian trixie). NOT Alpine — Docker Scout
# rates an Alpine base as 0-CVE, but that only measures the base layer; our
# CVEs ride inside glibc-built Go binaries (terraform/terragrunt/gcloud/aws),
# which will not run on musl without a glibc-compat shim. Net: Alpine trades
# real breakage for a cosmetic base score, so we stay on glibc Debian.
#
# `perl` carries a residual critical/high here, but `git` hard-depends on it
# (apt refuses to purge perl without removing git, which terrahawk needs to
# restore .terraform.lock.hcl). It is absorbed via periodic rebuilds as
# Debian ships the trixie-security patch — enforced by scripts/build-push.sh.
FROM python:3.13-slim AS runtime-base
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git openssh-client && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 65532 nonroot && \
    useradd  -u 65532 -g 65532 -d /home/nonroot -s /usr/sbin/nologin -m nonroot && \
    # Terraform plugin cache (TF_PLUGIN_CACHE_DIR points here in the final stage)
    mkdir -p /cache/plugins && chown -R 65532:65532 /cache

# ============================================================
# Final stage — AWS variant
# ============================================================
FROM runtime-base AS final-aws
COPY --from=binaries /tools/mise             /usr/local/bin/mise
COPY --from=binaries /tools/terraform        /usr/local/bin/terraform
COPY --from=binaries /tools/terragrunt       /usr/local/bin/terragrunt
COPY --from=binaries /usr/local/awscli        /usr/local/awscli
# Recreate the launcher symlinks — COPYing /usr/local/bin/aws dereferences the
# symlink into a standalone file, and the PyInstaller launcher then fails to
# find its bundled libpython relative to /usr/local/bin.
RUN ln -s /usr/local/awscli/v2/current/bin/aws           /usr/local/bin/aws && \
    ln -s /usr/local/awscli/v2/current/bin/aws_completer /usr/local/bin/aws_completer
COPY --from=pip-builder /install/lib         /usr/local/lib
COPY --from=pip-builder /install/bin         /usr/local/bin
USER nonroot

# ============================================================
# Final stage — Azure variant
# ============================================================
FROM runtime-base AS final-azure
COPY --from=binaries /tools/mise             /usr/local/bin/mise
COPY --from=binaries /tools/terraform        /usr/local/bin/terraform
COPY --from=binaries /tools/terragrunt       /usr/local/bin/terragrunt
COPY --from=pip-builder /install/lib         /usr/local/lib
COPY --from=pip-builder /install/bin         /usr/local/bin
USER nonroot

# ============================================================
# Final stage — GCP variant
# ============================================================
FROM runtime-base AS final-gcp
COPY --from=binaries /tools/mise             /usr/local/bin/mise
COPY --from=binaries /tools/terraform        /usr/local/bin/terraform
COPY --from=binaries /tools/terragrunt       /usr/local/bin/terragrunt
COPY --from=binaries /opt/google-cloud-sdk   /opt/google-cloud-sdk
# Only gcloud is linked — gsutil/bq are stripped from the SDK above (unused by
# terrahawk, and their bundled binaries carried critical/high CVEs).
RUN ln -s /opt/google-cloud-sdk/bin/gcloud  /usr/local/bin/gcloud
COPY --from=pip-builder /install/lib         /usr/local/lib
COPY --from=pip-builder /install/bin         /usr/local/bin
USER nonroot

# ============================================================
# Select the final image based on CLOUD and add shared config
# ============================================================
FROM final-${CLOUD}
ARG CLOUD
LABEL org.opencontainers.image.title="terrahawk" \
    terrahawk.cloud="${CLOUD}"

COPY terrahawk.py            /app/terrahawk.py
COPY src/terrahawk/          /app/src/terrahawk/
COPY THIRD_PARTY_LICENSES   /app/THIRD_PARTY_LICENSES

ENV TF_PLUGIN_CACHE_DIR=/cache/plugins \
    TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=true \
    MISE_DATA_DIR=/home/nonroot/.local/share/mise \
    MISE_CACHE_DIR=/home/nonroot/.cache/mise \
    MISE_YES=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TERRAHAWK_CLOUD=${CLOUD}

WORKDIR /workspace

ENTRYPOINT ["python3", "/app/terrahawk.py"]
CMD ["--help"]
